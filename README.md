# FPL Decision Engine

An autonomous decision-support system for a single Fantasy Premier League
(FPL) manager competing in a private head-to-head (H2H) league. It is
intended to eventually ingest player/fixture data, model expected points,
optimize squad/transfer/chip decisions, simulate H2H matchups, and explain
its recommendations — learning recursively from how past recommendations
actually performed.

**This repository is currently at Milestone 0 ("Repository and Automation
Foundation") only.** Almost none of the product functionality described
above exists yet. What exists today is the scaffolding: repo layout,
dependency management, a minimal FastAPI app with health checks, a job
execution framework with exactly one working job type (`system_health`),
Alembic migrations for a single `jobs` table, and three CI/automation
workflows. Every other subsystem (`app/ingestion`, `app/modeling`,
`app/optimization`, etc.) is an empty placeholder package.

## Architecture overview

```
apps/api/        FastAPI app (health endpoints only, so far)
apps/web/        Frontend (not started)
app/*/           Domain subpackages — all placeholders except app/jobs
app/jobs/        Job runner framework: JobRunner ABC, registry, execute_job service
packages/domain/ Framework-agnostic enums shared across the app (JobType, JobStatus, TriggerType)
packages/database/ SQLAlchemy Base, engine/session, health check, ORM models
packages/shared/ Settings (pydantic-settings) and JSON logging setup
migrations/      Alembic migrations (hand-written for Milestone 0)
config/          YAML config placeholders for future milestones
scripts/run_job.py  CLI to execute a single job synchronously
tests/           unit (no DB) / integration (real Postgres) / contract & e2e (stubs)
.github/workflows/  ci.yml, health.yml, scheduled-dispatcher.yml
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

This starts a `postgres:16-alpine` container and the API container (with
live reload). The API depends on Postgres passing its healthcheck before
starting. Once up: `curl http://localhost:8000/health` and
`curl http://localhost:8000/health/db`.

> **Note:** Docker was not installed on the machine this repository was
> originally scaffolded on, so `docker compose up` and the integration test
> suite have been reviewed for correctness but not yet runtime-validated.
> See "Known limitations" in the Milestone 0 completion report for details.

## Database setup

Locally without Docker, point `DATABASE_URL` in `.env` at any reachable
Postgres 16 instance, then:

```bash
uv run alembic upgrade head
```

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

## GitHub setup

**No GitHub secrets are required for Milestone 0.** All three workflows
(`ci.yml`, `health.yml`, `scheduled-dispatcher.yml`) spin up their own
ephemeral, CI-local Postgres service container with fixed, non-sensitive
placeholder credentials — there is no persistent hosted database yet for
them to need real credentials for.

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

### How the scheduled dispatcher works

`scheduled-dispatcher.yml` runs hourly (cron `0 * * * *` — an explicit
Milestone-0 placeholder cadence, not real deadline-aware scheduling, which
lands in a later milestone). Each run stands up an ephemeral Postgres
container, applies migrations, then calls `scripts/run_job.py` **twice**
with the same idempotency key (derived from the current UTC hour, e.g.
`scheduled-test-2026-07-09T14`), and asserts the two calls returned the same
`job_id` — proving the idempotency mechanism actually prevents duplicate job
rows, not just in theory.

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
