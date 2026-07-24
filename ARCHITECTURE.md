# Architecture

Reference doc for anyone (human or agent) working in this repo. Written by
reading the actual code, not the existing docs — **README.md,
docs/operations.md, docs/learning-loop.md, docs/architecture/README.md, and
nearly every `app/*/__init__.py` docstring are stale**, frozen at
"Milestone 0/1" language ("placeholder", "empty as of Milestone 0") while
the real code has moved through roughly 8 milestones. Concretely: 17 of 18
`JobType` values have real, tested handlers; only `PROJECTION_GENERATION`
is genuinely unbuilt (deliberately -- no defined consumer/table to write
to, see below). **Do not infer capability from a
docstring's claim or a package's `__init__.py` — check `app/jobs/registry.py`
and grep for the actual code.** This file will itself go stale; when in
doubt, re-verify against the code the way this file was built (see
"Methodology" at the bottom).

## What this is

Autonomous decision-support for a single Fantasy Premier League manager in
a private H2H league: ingest player/fixture data, project points, optimize
squad/transfer/lineup/chip decisions, simulate H2H matchups, generate a
correlated recommendation with an explanation, and recursively learn from
how past recommendations actually performed.

## Tech stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 (sync, `psycopg[binary]`), Alembic, `uv`.
- **Frontend** (`apps/web`): Next.js 16 App Router + TypeScript, React 19, `openapi-fetch` against a generated client, Playwright (e2e) + Vitest/RTL (component tests).
- **DB**: Postgres 16.
- **Modeling**: scikit-learn (decision tree / random forest / gradient boosting) + hand-written baselines, `pulp` for the squad/lineup MILP solver.
- **CI**: GitHub Actions — `ci.yml` (lint/typecheck/unit/integration/migrations/security/docker-build, both Python and web) plus several `workflow_dispatch` jobs (see "Scheduling").

## Directory map

```
apps/api/            FastAPI app: routers, auth, schemas — the REST surface over the job system
apps/web/             Next.js dashboard
app/jobs/             JobRunner ABC, registry (JobType -> handler class), execute_job service
app/jobs/handlers/     One file per job type's real orchestration logic (see inventory below)
app/ingestion/         Live-FPL-API ingestion (service.py) + historical CSV-archive ingestion (historical_*.py)
app/rules/             bootstrap-static -> versioned RulesConfigSnapshot (extraction/diff/status)
app/data_quality/      Pure check functions + score/optimization-gate computation
app/features/          Leakage-safe per-(player, gameweek) feature engineering
app/modeling/          MODEL_TRAINING internals: baselines, sklearn candidates, walk-forward validation, promotion
app/optimization/      Squad/lineup MILP solver, constraints, projection formula, optimization gate
app/transfers/         Transfer scenario search, selling-price/free-transfer rules
app/chips/             Chip window resolution, user chip inventory, chip scenario evaluation
app/h2h/                Rival-state derivation, H2H matchup Monte Carlo simulation, strategy comparison
app/learning/           Recursive learning: outcome ingestion, counterfactuals, failure attribution, signals
packages/domain/        Framework-agnostic enums shared everywhere (JobType, ModelFamily, RulesStatus, ...)
packages/database/      Base, engine/session, all 44 ORM models
packages/fpl_client/    Resilient adapter over the LIVE public FPL API
packages/shared/        pydantic-settings Settings + JSON logging setup
migrations/             Hand-written Alembic migrations
scripts/run_job.py      CLI to execute one job synchronously (prints JSON result)
tests/unit/ (44 files)  No DB
tests/integration/ (46) Real Postgres, monkeypatched FPL client(s)
tests/contract/, tests/e2e/  Genuinely empty stubs (their own READMEs say so honestly)
tests/fixtures/          Committed FPL API samples (fpl_api/) + a small real historical CSV set (historical/2023-24/)
.github/workflows/        ci.yml, health.yml, ingest.yml, scheduled-dispatcher.yml, historical-ingest.yml, feature-generation.yml, train.yml
```

## Job system

Every unit of real work is a `Job` row + a `JobRunner` subclass registered
against a `JobType` in `app/jobs/registry.py`. `execute_job(session,
job_type, parameters, requested_by, trigger_type, idempotency_key=...)`
runs it **synchronously** — no queue, no polling infra. `scripts/run_job.py`
is the CLI entrypoint; `apps/api` routers call `execute_job` directly for
the job types that have an HTTP trigger.

### Full JobType inventory (ground truth, not the enum's own stale docstring)

| JobType | Handler file | Real? | HTTP trigger | Other trigger |
|---|---|---|---|---|
| `SYSTEM_HEALTH` | `system_health.py` | ✅ | — | `health.yml` (manual) |
| `RULES_SYNC` | `rules_sync.py` | ✅ | — | part of `DATA_INGESTION`'s own flow; `ingest.yml` |
| `DATA_INGESTION` | `data_ingestion.py` | ✅ | — | `make ingest`, `scheduled-dispatcher.yml` (cron `0 */4 * * *`), `ingest.yml` |
| `HISTORICAL_DATA_INGESTION` | `historical_data_ingestion.py` | ✅ | — | CLI / `historical-ingest.yml` (manual) only |
| `MANAGER_SYNC` | `manager_sync.py` | ✅ | `POST /managers/sync` | |
| `LEAGUE_SYNC` | `league_sync.py` | ✅ | `POST /leagues/sync` | |
| `FEATURE_GENERATION` | `feature_generation.py` | ✅ | — | `make features`, `feature-generation.yml`, `train.yml` |
| `MODEL_TRAINING` | `model_training.py` | ✅ | — | `make train`, `train.yml`; also triggered **in-process** by `GAMEWEEK_REVIEW` on a sustained MAE breach |
| `PROJECTION_GENERATION` | — | ❌ | — | genuinely unbuilt; `project_player`/`project_player_with_champion` in `app/optimization/projection.py` are called directly by other jobs instead |
| `SQUAD_OPTIMIZATION` | `squad_optimization.py` | ✅ | `POST /optimize/initial-squad` | |
| `LINEUP_OPTIMIZATION` | `lineup_optimization.py` | ✅ | `POST /optimize/lineup` | |
| `TRANSFER_OPTIMIZATION` | `transfer_optimization.py` | ✅ | `POST /optimize/transfers` | |
| `CHIP_SIMULATION` | `chip_simulation.py` | ✅ | `POST /optimize/chips` | |
| `H2H_SIMULATION` | `h2h_simulation.py` | ✅ | `POST /h2h/simulate` | |
| `RECOMMENDATION_GENERATION` | `recommendation_generation.py` | ✅ | `POST /recommendations/generate` | |
| `GAMEWEEK_REVIEW` | `gameweek_review.py` | ✅ | `POST /jobs/review-gameweek` | |
| `BACKTEST` | `backtest.py` | ✅ | `POST /jobs/backtest` | |
| `FULL_PIPELINE` | `full_pipeline.py` | ✅ | — | `make full-pipeline` |

All optimization/simulation/recommendation jobs are **synchronous request/response** — `execute_job` blocks until done and the HTTP endpoint returns the full result in one call. No job-id polling exists anywhere in the API.

`FULL_PIPELINE` (`full_pipeline.py`) is a pure convenience orchestrator, not
a new capability: chains `DATA_INGESTION` -> `FEATURE_GENERATION` (scoped to
just the season `DATA_INGESTION` touched, not `"all"` -- `MODEL_TRAINING`
auto-discovers every season that already has features from any prior run,
so re-processing historical seasons on every routine call would be pure
waste) -> `MODEL_TRAINING`, each via `execute_job` on its own dedicated
session (mirrors `GameweekReviewJobRunner`'s own established precedent for
one job type triggering another). Fails fast at the first failing step;
`output_json["steps"]` records every step attempted, including on failure.
Deliberately excludes `RECOMMENDATION_GENERATION` -- that job only
correlates the 5 recommendation-producing jobs' already-persisted runs, and
`FULL_PIPELINE` never produces any of those, so appending it would just
correlate whatever (if anything) already exists, not a meaningful pipeline
step. CLI/`make full-pipeline` only, no HTTP trigger (matches
`FEATURE_GENERATION`/`MODEL_TRAINING`'s own precedent of being
CLI/workflow-triggered, not API-triggered).

### The optimization gate

`app/optimization/gate.check_optimization_gate(session, season_id)` is a
hard, all-or-nothing veto in front of `SQUAD_OPTIMIZATION`/
`LINEUP_OPTIMIZATION`/`TRANSFER_OPTIMIZATION`/`CHIP_SIMULATION` (not
`H2H_SIMULATION`, which has its own softer off-season fallback logic).
Refuses if the most recent terminal `DATA_INGESTION` run's
`optimization_permitted` flag is `False` (itself gated by
`app/data_quality/scoring.py`: any CRITICAL issue, `BLOCKED` rules status,
or `BLOCKED` overall endpoint health vetoes outright; otherwise requires
score ≥ 70). **No override parameter exists on any of the four gated
endpoints** — a blocked gate creates no `optimization_runs` audit row at
all. A common CRITICAL trigger: zero gameweeks marked `is_current` (true
for the whole pre-season window before FPL marks GW1 live).

`POST /optimize/preview` (added post-review) is a deliberate, separate
escape hatch for that specific pre-season window — it never calls
`check_optimization_gate` at all, reuses the same solver/projection code
as `SQUAD_OPTIMIZATION`, and never persists anything (no `Job`, no
`optimization_runs` row) so it can never be confused with a real run. Its
response includes `is_cold_start_gameweek`: when the target gameweek is
the season's own first (zero `player_gameweek_stats` rows exist for that
season yet), the champion model's projections are systematically too low
in magnitude (though rankings are usually still sensible) — every
rolling-form feature it was trained on is blank for a brand-new season's
GW1, an input shape no training row ever had. Confirmed live: champion
model projected Haaland at 0.43 pts for a cold-start GW1 vs. FPL's own
`ep_next` of 4.0 for the same player.

## Two separate ingestion pipelines — don't conflate them

This is the single most likely thing to get missed (it was, this session,
until the user pushed back):

1. **`DATA_INGESTION`** (`app/ingestion/service.py`) — the **live, current
   season only**. Hits the real `fantasy.premierleague.com/api`
   (`bootstrap-static`, `fixtures`, `event-status`) via
   `packages/fpl_client/client.py`. This is what `make ingest` and the
   4-hourly scheduled dispatcher run. Produces exactly the season's own
   `players`/`teams`/`gameweeks`/`fixtures` rows plus a `rules_config`
   snapshot.

2. **`HISTORICAL_DATA_INGESTION`** (`app/ingestion/historical_service.py` +
   `historical_client.py` + `historical_normalization.py`) — **past,
   completed seasons**, sourced from `raw.githubusercontent.com/vaastav/
   Fantasy-Premier-League` (a long-running, MIT-licensed, actively
   maintained *scraper/repackaging* of FPL data — not an official FPL
   source; treated as personal decision-support use only, never
   redistribution, per that module's own docstring). `EARLIEST_USABLE_SEASON
   = "2020-21"` — seasons 2016-17 through 2019-20 use an incompatible OPTA-
   derived CSV schema and are permanently excluded. Fetches 4 CSVs per
   season (`teams.csv`, `players_raw.csv`, `fixtures.csv`,
   `gws/merged_gw.csv`) and normalizes them into the **exact same tables**
   `DATA_INGESTION` writes to — `seasons`, `teams`, `players`, `gameweeks`,
   `fixtures`, and critically **`player_gameweek_stats`** (real per-
   gameweek stats *with* `fixture_id`, so opponent/home-away context is
   fully present via the normal `Fixture.home_team_id`/`away_team_id`
   relationship — this data source genuinely answers "how did this player
   do at home vs. this specific opponent"). Trigger:
   `{"season_label": "2023-24"}` for one season, or `{"season_label":
   "all"}` to backfill every usable season up to (but not including) the
   current one in a single job call. No API/UI trigger exists — CLI or the
   `historical-ingest.yml` workflow only. Verified live this session: ~14s
   per season, ~80s for a full 6-season (2020-21..2025-26) backfill,
   ~24-30k `player_gameweek_stats` rows per season.

Because `HISTORICAL_DATA_INGESTION` populates the same `player_gameweek_stats`
table `FEATURE_GENERATION`/`MODEL_TRAINING` already consume (both accept a
`season_labels`/`season_label` parameter, not hardcoded to "current
season"), backfilling history and then running `FEATURE_GENERATION` +
`MODEL_TRAINING` is a real, working path to a trained champion model
**before the live season has produced any of its own gameweek data** — no
new code needed, just running the existing pipeline.

## Modeling / learning loop

- **`app/features`**: `compute_features_for_player_gameweek` builds a
  leakage-safe feature row per (player, gameweek) — rolling form, next-
  fixture difficulty (from `Fixture.home_difficulty`/`away_difficulty`,
  themselves derived from FPL's own `Team.strength_*` ratings, which persist
  season-to-season), etc. Persisted to `player_features`.
- **`app/modeling`**: `MODEL_TRAINING` walk-forward-validates 7 candidate
  `ModelFamily` values — 4 pure-formula baselines
  (`BASELINE_SEASON_AVERAGE`/`BASELINE_RECENT_AVERAGE`/
  `BASELINE_WEIGHTED_RECENT_AVERAGE`/`BASELINE_RULES_EP_NEXT`, no `.fit()`,
  no artifact) and 3 real sklearn regressors
  (`DECISION_TREE`/`RANDOM_FOREST`/`GRADIENT_BOOSTING`, fit against
  `player_features`, persisted as `.joblib`). Winner becomes a
  `model_versions` row; **`is_champion`** is an enforced invariant
  (`is_champion == (lifecycle_state == CHAMPION)`, exactly one champion at a
  time, no season scoping — a global concept). `GET /models/champion`
  surfaces it; `app/optimization/projection.project_player_with_champion`
  uses it when present, falling back to the plain heuristic
  (`ep_next` → `points_per_game` → 0) otherwise.
- **`app/learning`** (Milestone 7, "recursive learning"): after a
  recommendation's outcome is known, `outcome_ingestion.py` records actuals,
  `counterfactuals.py` compares the followed recommendation against
  alternatives that weren't taken, `attribution.py` classifies *why* a miss
  happened (`FailureReasonCode`/`FailureCategory`), `signals.py` computes
  rolling-window signals (`LearningSignalType`, e.g.
  `ROLLING_PROJECTION_MAE`). `GAMEWEEK_REVIEW` ties this all together and,
  on a **sustained** `ROLLING_PROJECTION_MAE` breach, triggers `MODEL_TRAINING`
  in-process (not via a separate scheduled workflow) plus a promotion-gate
  check — see `gameweek_review.py`'s own docstring for the exact
  failure-isolation boundary (a retraining failure never fails the review
  itself).

## Data model — key relationships

45 ORM models under `packages/database/models/`. The load-bearing shape:

- **`seasons`** is the root scope for almost everything (`players`, `teams`,
  `positions`, `gameweeks`, `fixtures`, `rules_config`, `player_gameweek_stats`,
  `player_features`, `manager_squad_snapshots`, `optimization_runs`,
  `transfer_optimization_runs`, `chip_optimization_runs`, `rival_profiles`,
  `recommendation_audits`, `learning_signals`, `learning_reports`,
  `model_challenger_evaluations` all carry a direct `season_id` FK). Exactly
  one `seasons` row should have `is_current=True` at a time — nothing
  enforces this at the DB level, it's an application-level invariant.
- **`jobs`** is referenced *by* nearly every run/audit table (`job_id`), never
  the reverse — jobs are a flat audit log, not a parent entity with owned
  children in the FK-cascade sense.
- **`manager_squad_snapshots`**: insert-only audit trail. `source` is either
  `MANAGER_SYNC` (real, `is_current` meaningful) or `MANUAL_ENTRY` (never
  `is_current`, no `raw_picks_json`, deletable via `DELETE /squads/{id}` —
  the only deletable row in this whole schema; blocked with 409 if a
  transfer/chip/H2H run still references it, since none of those FKs cascade).
- **`model_versions`** is deliberately **not** season-scoped (global,
  `training_job_id` → `jobs` only) — a champion trained on historical data
  applies regardless of which season is "current".
- **`h2h_simulation_runs`** — "H2H" throughout this codebase means **fantasy
  manager vs. fantasy manager** (this app's own H2H league mechanic), never
  real football club-vs-club history. It references `manager_squad_snapshot_id`
  + `rival_profile_id`, and needs *a* starting XI for both sides (solved via
  `_derive_starting_and_bench_from_solver` when the user's own snapshot has
  no `raw_picks_json`; the rival's side is never solved on their behalf).
- No FK in this schema is `ON DELETE CASCADE` — every relationship is `NO
  ACTION`. Bulk cleanup (e.g. removing synthetic test-season data) needs an
  explicit, manually-ordered multi-table delete, or `session_replication_role
  = replica` to bypass FK-order requirements for a single controlled cleanup
  transaction (done once already this session).
- **`player_price_history`** (added post-review, migration `0026`):
  insert-only, one row per OBSERVED `players.now_cost` change.
  `app.ingestion.normalization.upsert_players` compares each incoming
  `now_cost` against what's already stored for that
  `(season_id, fpl_element_id)` *before* the bulk upsert runs, and appends a
  row only on a real difference — never once per `DATA_INGESTION` run. Not
  wired into `HISTORICAL_DATA_INGESTION` (a past season's price is static,
  there's nothing to observe changing). Exposed via
  `GET /players/{id}/price-history` (most-recent-first, capped at 200,
  empty list — not a 404 — when nothing's changed yet) and shown on the
  player detail page.

## API (`apps/api`)

Single FastAPI app, single shared-secret auth (`apps/api/auth.py`,
`require_shared_token`, `secrets.compare_digest`, fails closed with 401) on
every router except `health.py`. No user table — deliberate, this is a
single-manager system. CORS via `Settings.cors_allowed_origins_list`
(empty/fail-closed by default). Routers: `players`, `optimize` (squad/
lineup/transfers, plus `chips` sharing the same `/optimize` prefix — watch
route registration order in `main.py`, the catch-all `GET
/optimize/{id}` must be registered after every static sibling path or it
swallows them), `managers`/`squads`, `h2h`, `recommendations`, `jobs`/
`learning`, `leagues`, `models`. List endpoints are capped-at-200,
most-recent-first (no real pagination) except `players`, which has real
page/page_size.

## Frontend (`apps/web`)

Next.js App Router. Two distinct API-calling paths:
- **Server Components** call the backend directly via `lib/api-client.ts`'s
  `backendClient()` (server-to-server, holds the real `APP_SHARED_TOKEN`).
- **Client Components** ("use client") call `lib/browser-client.ts`'s
  `browserClient()`, which hits this app's own `/api/proxy/[...path]`
  route (GET/POST/DELETE) — the browser never holds the real token, only a
  signed session cookie (`lib/session.ts`, HMAC-SHA256, `proxy.ts` gates
  every non-public path).

Both share one generated `lib/api-types.ts` (from the backend's own
OpenAPI schema — regenerate after *any* `apps/api` schema/router change,
see README's exact command). Pages (14 total): Dashboard, Team (+ manual
squad create/duplicate/delete), Players (list + detail w/ live
projection), Rivals (+ H2H simulate), History (all 4 run types), Learning
(review + signals/failures + backtest), plus one detail page per run type
(`squad/[id]`, `transfers/[id]`, `chips/[id]`, `h2h/[id]`, `backtest/[id]`).
Light theme is `prefers-color-scheme` only (no toggle); tables wrap in a
`TableScroll` component for mobile.

Test suites (both committed real devDependencies, not ad-hoc scripts):
`apps/web/e2e/` (Playwright, `npm run test:e2e`, drives the real installed
Chrome, `webServer` auto-starts `next dev --port 3002` against the already-
running docker api) and Vitest+RTL component tests co-located as
`*.test.tsx` (`npm run test:unit`). Neither is wired into `ci.yml`'s `web`
job (lint/typecheck/build only) — the e2e suite deliberately isn't, since
seeding real UI data needs either the live rate-limited FPL API or the
pytest-only monkeypatched client, neither CI-appropriate as-is (see
`apps/web/e2e/README.md`).

## Testing conventions

- `tests/unit/` (44 files): no DB, no network.
- `tests/integration/` (46 files): real Postgres, monkeypatched FPL client(s)
  (`tests/integration/conftest.py` has ~6 different `patched_fpl_client_*`
  fixtures for different scenarios — optimization pool, league/H2H, manager
  endpoints). **Never truncated between runs** — this is a deliberate,
  established discipline: tests must be robust against a shared, growing
  Postgres (unique-per-run IDs via `secrets.randbelow`/season-year counters,
  `ORDER BY` on every query that could otherwise be non-deterministic,
  assert "my own row is present/correct" rather than exact list length).
  Run the integration suite **twice** before considering backend work done
  — this repo's own established protocol, catches order-dependent flakiness
  a single run wouldn't.
- **Tests MUST run against `fpl_decision_engine_test`, never the real
  `fpl_decision_engine` dev database** — always `source .env.test` (never
  `.env`) before any `pytest`/`scripts/run_job.py` invocation used for
  testing/experimentation. This is not a style preference: it caused real
  damage once (see the optimization-gate section above and CLAUDE.md) —
  `DATA_INGESTION`/`HISTORICAL_DATA_INGESTION` globally unset
  `seasons.is_current` on every row before setting their own season
  current, so a test run against the wrong database silently demoted the
  real current season and every live page started showing synthetic
  ("Player1", "Player2", ...) data instead. Setup: `CREATE DATABASE
  fpl_decision_engine_test` on the same Postgres instance, then `DATABASE_URL=...test uv run alembic upgrade head` once.
- `tests/contract/`, `tests/e2e/` (top-level, Python): genuinely empty,
  their own READMEs honestly say "populated starting a later milestone" —
  these are the one place the "stale docs" warning does NOT apply.
- `tests/fixtures/fpl_api/`: trimmed real API response samples. `tests/
  fixtures/historical/2023-24/`: a small real historical CSV sample for
  testing the historical pipeline without hitting GitHub.

## Known real gaps (as of this doc)

- `PROJECTION_GENERATION` job type: enum member only, no handler, deliberately -- no defined
  consumer/table for it to write to (`project_player`/`project_player_with_champion` are
  already called directly by every job that needs a projection, computed live, never cached).
- No official-API season-aggregate history (`element-summary/{id}/history_past`) ingestion — moot in practice since `HISTORICAL_DATA_INGESTION` already provides strictly richer (gameweek-level, not just season-total) historical data from a different source.

`FULL_PIPELINE` is no longer a gap (see the job-inventory table above) and
the Makefile's `recommend`/`review-gameweek`/`backtest`/`full-pipeline`
targets are now real, individual targets (previously combined into one
stub target printing "not yet implemented" for all four, even though 3 of
them already had working handlers).

## Methodology (how this doc was produced, and how to keep it honest)

Built by: reading `app/jobs/registry.py`'s real `@register_handler` calls
(not the `JobType` enum's own docstring), grepping every `apps/api/routers/
*.py` for actual route decorators (not assuming from file names), reading
`.github/workflows/*.yml` `on:` triggers directly, and opening real
implementation files rather than trusting `app/*/__init__.py` docstrings —
nearly all of which still say "Placeholder — implemented in a later
milestone" for packages that are fully built (`app/ingestion`,
`app/optimization`, `app/rules`, `app/data_quality`, `app/transfers` all
have this stale placeholder despite being real). When updating this file,
re-verify against code the same way — don't just patch based on a commit
message or a docstring's claim.
