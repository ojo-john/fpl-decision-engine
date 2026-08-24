# Architecture

Reference doc for anyone (human or agent) working in this repo. Written by
reading the actual code, not the existing docs — **README.md,
docs/operations.md, docs/learning-loop.md, docs/architecture/README.md, and
nearly every `app/*/__init__.py` docstring are stale**, frozen at
"Milestone 0/1" language ("placeholder", "empty as of Milestone 0") while
the real code has moved through roughly 8 milestones. Concretely: 18 of 19
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
app/scheduling/         Real, stateless scheduling logic (run_tick) -- called by both scripts/scheduler.py's local loop and POST /internal/scheduler-tick (see "Local scheduler"/"Cloud deployment" sections below)
app/notifications/      Real Web Push sending (send_push_to_all) -- see "Proactive push alerting" section below
packages/domain/        Framework-agnostic enums shared everywhere (JobType, ModelFamily, RulesStatus, ...)
packages/database/      Base, engine/session, all 52 ORM models
packages/fpl_client/    Resilient adapter over the LIVE public FPL API
packages/shared/        pydantic-settings Settings + JSON logging setup
migrations/             Hand-written Alembic migrations
scripts/run_job.py      CLI to execute one job synchronously (prints JSON result)
scripts/export_notes_snapshot.py  CLI to regenerate .github/state/known-notes-snapshot.json from the live API (see scout-watch.yml section)
scripts/post_deadline_smoke_test.py  CLI to verify real-data preconditions for manager_sync/chip inventory/league_sync/rival profiles/differentials/price calibration (see "Post-deadline real-data verification" section below and docs/operations.md)
scripts/scheduler.py     Local background loop: thin wrapper around app.scheduling.tick.run_tick (see "Local scheduler" section below)
render.yaml              Render Blueprint for the backend API's real cloud deployment (see "Cloud deployment" section below, docs/deployment.md)
docs/deployment.md       Real, ordered free-tier cloud hosting setup checklist (Supabase/Render/Vercel/cron-job.org)
tests/unit/ (60 files)  No DB
tests/integration/ (60) Real Postgres, monkeypatched FPL client(s)
tests/contract/, tests/e2e/  Genuinely empty stubs (their own READMEs say so honestly)
tests/fixtures/          Committed FPL API samples (fpl_api/) + a small real historical CSV set (historical/2023-24/)
.github/workflows/        ci.yml, health.yml, ingest.yml, scheduled-dispatcher.yml, historical-ingest.yml, feature-generation.yml, train.yml, league-watch.yml, scout-watch.yml (both standalone -- see their own sections below)
.github/state/            current-squad.json, known-notes-snapshot.json (both consumed by scout-watch.yml; league-814682-watch.json consumed by league-watch.yml)
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
| `DATA_INGESTION` | `data_ingestion.py` | ✅ | `POST /data/refresh` | `make ingest`, `scheduled-dispatcher.yml` (cron `0 */4 * * *`), `ingest.yml` |
| `HISTORICAL_DATA_INGESTION` | `historical_data_ingestion.py` | ✅ | — | CLI / `historical-ingest.yml` (manual) only |
| `MANAGER_SYNC` | `manager_sync.py` | ✅ | `POST /managers/sync` | |
| `LEAGUE_SYNC` | `league_sync.py` | ✅ | `POST /leagues/sync` | |
| `TEAM_STRENGTH_SYNC` | `team_strength_sync.py` | ✅ | — | CLI (`scripts/run_job.py`) only, no scheduled trigger yet |
| `FEATURE_GENERATION` | `feature_generation.py` | ✅ | — | `make features`, `feature-generation.yml`, `train.yml` |
| `MODEL_TRAINING` | `model_training.py` | ✅ | — | `make train`, `train.yml`; also triggered **in-process** by `GAMEWEEK_REVIEW` on a sustained MAE breach |
| `START_PROBABILITY_MODEL_TRAINING` | `start_probability_training.py` | ✅ | `POST /models/start-probability-training` | a SEPARATE, additive auxiliary classifier — never touches `model_versions`/the champion; see "Modeling / learning loop" below |
| `PROJECTION_GENERATION` | — | ❌ | — | genuinely unbuilt; `project_player`/`project_player_with_champion` in `app/optimization/projection.py` are called directly by other jobs instead |
| `SQUAD_OPTIMIZATION` | `squad_optimization.py` | ✅ | `POST /optimize/initial-squad` | |
| `LINEUP_OPTIMIZATION` | `lineup_optimization.py` | ✅ | `POST /optimize/lineup` | |
| `TRANSFER_OPTIMIZATION` | `transfer_optimization.py` | ✅ | `POST /optimize/transfers` | |
| `CHIP_SIMULATION` | `chip_simulation.py` | ✅ | `POST /optimize/chips` | |
| `H2H_SIMULATION` | `h2h_simulation.py` | ✅ | `POST /h2h/simulate` | |
| `RECOMMENDATION_GENERATION` | `recommendation_generation.py` | ✅ | `POST /recommendations/generate` | |
| `GAMEWEEK_REVIEW` | `gameweek_review.py` | ✅ | `POST /jobs/review-gameweek` | |
| `BACKTEST` | `backtest.py` | ✅ | `POST /jobs/backtest` | |
| `COLD_START_PROJECTION_VALIDATION` | `cold_start_projection_validation.py` | ✅ | `POST /jobs/cold-start-validation` | CLI (`scripts/run_job.py --job-type cold_start_projection_validation`) |
| `POSITION_SEGMENTATION_VALIDATION` | `position_segmentation_validation.py` | ✅ | — | CLI (`scripts/run_job.py`) only, no HTTP trigger yet |
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
as `SQUAD_OPTIMIZATION` (`app.jobs.handlers.squad_optimization._build_candidates`),
and never persists anything (no `Job`, no `optimization_runs` row) so it
can never be confused with a real run. Its response includes
`is_cold_start_gameweek`: when the target gameweek is the season's own
first (zero `player_gameweek_stats` rows exist for that season yet), the
champion model's raw prediction is untrustworthy in magnitude (though
rankings are usually still sensible) — every rolling-form feature it was
trained on is blank for a brand-new season's GW1, an input shape no
training row ever had. Confirmed live: champion model projected Haaland
at 0.43 pts for a cold-start GW1 vs. FPL's own `ep_next` of 4.0 for the
same player.

**The cold-start check is per-player, not per-season** (post-review fix —
originally `_is_cold_start_season` alone decided this for every player
uniformly: zero `player_gameweek_stats` rows anywhere in the season. That
meant the moment even ONE player anywhere had one gameweek of real data
ingested, EVERY player switched from the cold-start blend to the champion
model's raw prediction — including players who themselves still had zero or
almost no current-season history of their own, e.g. a summer signing who
debuts a few gameweeks late, or a player returning from a long-term injury
mid-season — exactly the same all-rolling-features-`NaN` input shape the
original cold-start fix above was written to route around, still reachable
mid-season for any individually-absent player). `app/optimization/projection
.py`'s `_is_cold_start_player(session, player_id)` closes this: it's the
same "zero `player_gameweek_stats` rows" predicate, just scoped to one
player's own rows instead of the whole season's. `project_player_with_champion`
calls only `_is_cold_start_player` (not `_is_cold_start_season`) to decide —
a season-wide zero-rows state is a strict special case (implies every player
is individually cold-start too), so the per-player check alone is both
correct and sufficient there. `_is_cold_start_season`/`is_cold_start_season`
remain as their own functions, still consulted by `POST /optimize/preview`'s
`is_cold_start_gameweek` informational flag (a legitimately distinct,
season-wide "has this season produced any data at all yet" question) and by
the `/players/{player_id}/projection` breakdown below (`is_cold_start_season`
alongside the new `is_cold_start_player`). The threshold is a hard cutover
at exactly one row (no smoothed transition), deliberately mirroring the
original season-wide check's own all-or-nothing threshold: one real row is
enough to turn every rolling-window feature from `NaN` into a real value
(see `app.features.engineering`), and NaN-vs-real is the documented root
cause of the champion's under-prediction — sample-size/noise below that
point is a different problem this fix isn't scoped to solve.
`PROJECTION_FORMULA_VERSION` bumped to 3 for this — a real behavior change
to the formula's output, not a refactor (same reasoning `v2`'s own bump
documented for the original cold-start fix).

For a cold-start season, `app/optimization/projection.py`'s
`_cold_start_projection` replaces the raw champion prediction with a
blend: `(project_player(player).projected_points + career_avg) / 2` — a
50/50 average of FPL's own pre-season estimate and a real cross-season
scoring average for that specific player, resolved via the
season-independent `fpl_code`, populated by `HISTORICAL_DATA_INGESTION`
+ `app.features.service.compute_features_for_player_gameweek` (computed
on demand, not read from the persisted `player_features` table — see
below). `career_avg` here prefers
`career_avg_early_season_points_prior_seasons` (the player's average
across only each PRIOR season's own early gameweeks — `fpl_event_id <=
6` by default, matching `SQUAD_BUILD_HORIZON_LENGTH` — see
`app.features.engineering.career_avg_early_season_points_prior_seasons`,
recency-weighted ACROSS SEASONS since a follow-up post-review found the
original flat average gave a season 6 years ago exactly as much say as
last season: `0.5 ** (seasons_ago / DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS)`
per row, half-life 2.0 seasons by default. `seasons_ago` is a RANK among
the distinct seasons actually present for that player (0 = most recent
season present, 1 = one before that, ...), not a fixed calendar distance,
so a player's own missing seasons don't skew everyone else's ranking; all
rows within the same season still share one flat weight, only
cross-season recency decays)
over the plain full-season `career_avg_points_prior_seasons` whenever
early-season history exists; some players are well-established slow or
fast starters relative to their own season-long average (confirmed live
against real historical data: Haaland's early-season GW1-6 average is
10.21 vs. a 6.36 full-season average — a genuine fast-starter pattern a
full-season blend alone would have missed, changing the cold-start
blend from 5.18 to 7.11 for the same `ep_next`). Falls back to the
full-season average when no early-season-specific history exists (e.g.
every prior season on record started with a mid-season transfer), and
to the bare `project_player` estimate if no career history exists at
all (e.g. a debutant). That blend is then adjusted by
`next_fixture_difficulty` (FPL's own 1=easiest..5=hardest rating for the
specific target gameweek): `blended *= 1 + (3 - difficulty) * 0.075`,
i.e. a neutral fixture (3) leaves it unchanged, the easiest (1) is +15%,
the hardest (5) is -15%. This exists because neither `ep_next` nor
either career average varies by gameweek, so without it every gameweek
in a horizon summed to identical values regardless of the actual
fixture list. Both `0.5` (the blend ratio) and `0.075` (the difficulty
coefficient) are now named module-level constants in `projection.py`
(`COLD_START_BLEND_CAREER_AVG_WEIGHT`/`COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT`),
not inline magic numbers — see "Cold-start-projection historical
validation" below for why.

### Cold-start-projection historical validation

`_cold_start_projection`'s hand-written blend formula was never checked
against real historical outcomes before Milestone 8: `BACKTEST` and
`MODEL_TRAINING`'s own walk-forward validation both only ever evaluate a
trained `model_versions` row (`grep -rn "cold_start"
app/modeling/walk_forward.py app/jobs/handlers/backtest.py
app/jobs/handlers/model_training.py` returns zero matches), and this
formula isn't one. Found live, mid-session, while reviewing a real squad
decision: `DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS`'s cross-season
recency-weighting visibly under-weighted a real player (Joao Pedro)'s
clear, monotonic season-over-season improvement (early-season PPG
0.17 → 3.00 → 3.17 → 6.17 across 4 seasons blending to 3.89, not
something closer to the two most recent seasons).

`COLD_START_PROJECTION_VALIDATION` (`app/jobs/handlers/cold_start_projection_validation.py`,
no HTTP trigger conflict with `BACKTEST` since it takes no
`model_version_id` — no trained model is involved, only a hand-written
formula) closes this gap: `app/modeling/cold_start_validation.py`'s
`build_player_season_observations` builds one shared, leakage-safe
observation set — for every usable historical season from the SECOND
one onward, for every player with real early-season (`fpl_event_id <=
6`) data that season AND at least one strictly-prior season on record
(cross-season resolved via `fpl_code`, prior data cut off via the real
production `filter_to_strictly_prior` against that season's own
earliest gameweek deadline — never a reimplementation of that leakage
guard) — then reuses it for three real, independent sweeps, each calling
the real, unmodified `career_avg_early_season_points_prior_seasons`
rather than reimplementing it:

- **Half-life** (`sweep_half_life_candidates`): isolated to the
  `career_avg_early_season_points_prior_seasons` component alone (`ep_next`
  is NOT involved at all — half-life only ever affects this one
  component of the blend).
- **Fixture-difficulty coefficient** (`sweep_difficulty_coefficient_candidates`):
  real per-gameweek granularity, using real historical fixture
  difficulty ratings (`fixtures.csv`'s own `team_h_difficulty`/
  `team_a_difficulty`, already normalized into `fixtures.home_difficulty`/
  `away_difficulty`) and real actual per-gameweek points — also no
  `ep_next` dependency.
- **ep_next/career_avg blend ratio** (`sweep_blend_ratio_candidates`):
  the one dimension that genuinely needs an `ep_next` signal, and the
  one genuine gap this validation could not close cleanly — verified,
  not assumed: `merged_gw.csv` (the per-gameweek historical source) has
  no `ep_next` column at all; `ep_next` exists only on the season-scoped
  `players` row (from `players_raw.csv`), a single snapshot near that
  season's own END, not a pre-season, summer-transfer-aware estimate
  like what the LIVE formula actually blends with. Used anyway, as the
  most recent strictly-prior season's own end-of-season `ep_next`, for
  real executable numbers — but every persisted run's own
  `blend_ratio_caveat` column carries this same warning, and this
  dimension's results were treated as directional, not conclusive,
  evidence for exactly that reason.

Persists one `cold_start_validation_runs` row per run (migration 0034;
`ColdStartValidationRunStatus`, same "only SUCCEEDED is ever actually
persisted" shape as `BacktestRunStatus`) with all three sweeps' full
results plus the "best" and "current-default's-own" MAE for each
dimension. HTTP: `POST /jobs/cold-start-validation` (all parameters
optional — unlike `BACKTEST`, this job's own module-level candidate sets
ARE a sensible default), `GET /learning/cold-start-validations`,
`GET /learning/cold-start-validations/{id}`.

**Real results** (run 2026-08-17 against the real dev database's full
historical backfill, 6 usable seasons 2020-21..2025-26, n=2348 real
(player, season) pairs for the half-life/blend-ratio sweeps, n=13954 real
per-gameweek pairs for the difficulty sweep): all three production
constants were LEFT UNCHANGED — each was already at or within noise of
its own sweep's empirical minimum:

| Dimension | Candidates swept | Real MAE range | Current default | Default's MAE | Best candidate's MAE |
|---|---|---|---|---|---|
| `DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS` | 0.5, 0.75, 1.0, 1.5, 2.0, 3.0 | 1.2421–1.2594 | 2.0 | 1.2434 | 1.2421 (half-life 1.5) |
| `COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT` | 0.0, 0.025, 0.05, 0.075, 0.1, 0.15 | 1.65454–1.65603 | 0.075 | 1.65454 | 1.65454 (0.05, effectively tied) |
| `COLD_START_BLEND_CAREER_AVG_WEIGHT` | 0.3, 0.4, 0.5, 0.6, 0.7 | 1.1043–1.1427 | 0.5 | 1.1043 | 1.1043 (0.5 itself) |

The half-life curve is genuinely flat (all 6 candidates within ~0.017
MAE of each other) — this does NOT contradict the real Joao Pedro
finding that motivated the sweep: a single global half-life helping one
fast-improving player and hurting another in roughly equal measure
washes out in an aggregate metric even though it's a real, visible
miscalibration for any one specific player. See
`app/features/engineering.py`'s `DEFAULT_EARLY_SEASON_HALF_LIFE_SEASONS`
comment and `app/optimization/projection.py`'s two constants' own
comments for this same evidence inline at the code.

`app/optimization/projection.py:PROJECTION_FORMULA_VERSION` was NOT
bumped by this work — no constant's VALUE changed, only their names (bare
magic numbers → `COLD_START_BLEND_CAREER_AVG_WEIGHT`/
`COLD_START_DIFFICULTY_ADJUSTMENT_COEFFICIENT`) and the fact that they
are now empirically checked rather than unchecked; `_cold_start_projection`'s
actual output is unchanged for every input.

`career_avg_early_season_points_prior_seasons` is also a column on the
PERSISTED `player_features` table (migration 0030) for
`FEATURE_GENERATION`/`MODEL_TRAINING`/audit purposes, alongside the
existing `career_avg_points_prior_seasons` — but `_cold_start_projection`
itself never reads that persisted table; it always computes fresh via
`compute_features_for_player_gameweek`, so the cold-start blend
improvement is live immediately, without needing `FEATURE_GENERATION` to
be re-run. A re-run is only needed to backfill the new column on
already-computed historical `player_features` rows.

#### Price-informed fallback for true-rookie players (zero prior-season history)

A real, distinct gap in `_cold_start_projection` beyond the half-life/
difficulty/blend-ratio calibration above: when `career_avg` is `None` —
BOTH the early-season and full-season career averages are unavailable,
meaning this player has literally ZERO strictly-prior-season
`player_gameweek_stats` rows at all (a genuinely new-to-FPL signing or
promoted-team debutant, not just a player missing an early-season
window) — the function used to return `project_player`'s own `ep_next`
estimate completely untouched. This is the single least-informed
population in the whole formula getting the LEAST scrutiny, not more;
`build_player_season_observations` (above) structurally EXCLUDES this
population (it requires non-empty `prior_stats`), so it was invisible to
the original validation work too.

`app/modeling/cold_start_validation.py` adds a parallel, disjoint
observation set and evaluation path for exactly this population:

- `build_true_rookie_observations` — the mirror-image filter of
  `build_player_season_observations`: requires EMPTY `prior_stats`
  instead of non-empty. For price/position, uses this player's own
  EARLIEST early-season `player_gameweek_stats` row in the TARGET season
  itself (`value_at_time`/`position_short_name_at_time`) — mirroring
  `app.features.service.compute_features_for_player_gameweek`'s own
  documented precedent for this exact "no strictly-prior row" case: a
  player's price/position going into a gameweek deadline is public,
  pre-deadline-knowable information, not leakage.
- `compute_price_comparable_buckets` — the LIVE artifact:
  position-scoped price-quantile buckets (5 per position,
  `PRICE_COMPARABLE_BUCKETS_PER_POSITION`, collapsing to fewer/larger
  buckets if a position has less real data than that), each with a real
  average early-season points value, POOLED across every usable
  historical season (not leakage-scoped — this predicts a FUTURE season,
  the same "train on everything available" principle the champion model
  itself already follows for live predictions). Persisted to
  `cold_start_validation_runs.price_comparable_buckets_json`;
  `app.optimization.projection._load_current_price_comparable_buckets`
  loads the most recent row's copy at prediction time, mirroring
  `load_current_champion`'s own "load the most recent real artifact,
  degrade gracefully if none exists yet" precedent (`[]` — never raises
  — if `COLD_START_PROJECTION_VALIDATION` has never run).
- `sweep_price_prior_blend_weight_candidates` — real MAE/Spearman for
  blending the bucket estimate with an ep_next proxy, per candidate
  weight (`{0.0, 0.3, 0.5, 0.7, 1.0}`). Leakage-safe: unlike the live
  artifact, evaluating target season S here uses buckets built ONLY from
  seasons strictly before S — one bucket table per eval season, rebuilt
  fresh, mirroring `build_player_season_observations`'s own per-season
  discipline. **A real, WORSE caveat than the existing blend-ratio
  sweep's own**: a true rookie has no prior season at all, so there is no
  leakage-safe prior-season `ep_next` to fall back on the way the main
  blend-ratio sweep does — the only resolvable proxy is the TARGET
  season's own end-of-season `ep_next` snapshot, meaning the
  `weight=0.0` control here is compared against a number with
  substantially MORE hindsight than a genuinely pre-season `ep_next`
  would have. This makes `weight=0.0`'s own MAE optimistic — biased IN
  FAVOR of the pre-existing (do-nothing) behavior — so a result showing
  the price prior beating it anyway is conservative, not inflated,
  evidence.

`app.optimization.projection._price_informed_true_rookie_projection`
replaces the old "return baseline untouched" branch: looks up this
player's bucket via `feature_row.price_at_time`/`position_short_name`
(both already computed by the existing `compute_features_for_player_gameweek`
call `_cold_start_projection` makes — no extra query needed beyond the
bucket-table lookup itself), blends at `COLD_START_PRICE_PRIOR_BLEND_WEIGHT`,
then applies the SAME fixture-difficulty adjustment every other
cold-start branch already gets. Falls back to `baseline` untouched
(never raises) if price/position can't be resolved or no bucket table
has ever been computed yet.

New `cold_start_validation_runs` columns (migration 0039, chained after
0038): `n_true_rookie_observations`,
`price_prior_sweep_json`, `default_price_prior_weight`/`_mae`,
`best_price_prior_weight`/`_mae`, `price_prior_caveat`,
`price_comparable_buckets_json`. `COLD_START_PROJECTION_VALIDATION`'s
own `run()` now also builds this second observation set and persists
these fields on the same row, never allowed to fail the job's main three
sweeps if the true-rookie population happens to be empty for the
currently-usable seasons.

**Real results** (run 2026-08-17 against the real dev database, n=735
real true-rookie observations): `weight=0.0` (pure `ep_next`, the
PRE-this-feature behavior) had real MAE 1.0067; `0.3`/`0.5`/`0.7`/`1.0`
had 0.8673/0.8178/0.8031/0.8417 — LOWEST at `weight=0.7`, roughly a 20%
MAE reduction versus `ep_next` alone, despite the control being
optimistically biased in `ep_next`'s favor per the caveat above.
`COLD_START_PRICE_PRIOR_BLEND_WEIGHT` was set to **0.7** (a real,
evidence-backed change — unlike the three constants above, which were
left unchanged because the evidence didn't support moving them).

**ClubElo-based fixture strength — real infrastructure, real negative result for the cold-start
formula specifically.** `TEAM_STRENGTH_SYNC` (`app/jobs/handlers/team_strength_sync.py`) pulls
real, continuously-updated Elo ratings from ClubElo.com (`http://api.clubelo.com`, confirmed
live, free, no API key) via `packages/clubelo_client` (mirrors `packages.fpl_client`'s own
timeout/retry/backoff resilience conventions), maps FPL's team names onto ClubElo's own naming
via an explicit, hand-verified table (`app/ingestion/team_strength.py` — several real divergences:
"Man Utd"→"Man United", "Spurs"→"Tottenham", "Nott'm Forest"→"Forest", "Coventry City"→"Coventry",
"Hull City"→"Hull", "Ipswich Town"→"Ipswich"), and upserts `teams.elo_rating`/
`teams.elo_last_synced_at` (migration 0040). `player_features.next_fixture_elo_differential`
(this player's team's Elo minus their next opponent's, computed identically to
`next_fixture_difficulty` in `app/features/service.py`) is additive-only, so `FEATURE_VERSION` is
not bumped.

The motivating hypothesis was that this continuously-updated real signal would beat FPL's own
coarse, static 1-5 `next_fixture_difficulty` inside `_cold_start_projection`'s fixture-adjustment
multiplier. Real validation (`scripts/validate_elo_fixture_adjustment.py`, read-only against the
real dev database, real ClubElo historical snapshots, n=3035 real player-gameweek observations
from the 2025-26 season's own GW1-6) found the OPPOSITE: every tested Elo-based adjustment scale
(0.0 through 1.0) produced a WORSE MAE than the existing FPL-difficulty adjustment (best:
1.5592 at FPL-difficulty; Elo scale 0.0 i.e. no adjustment: 1.5665; every higher scale
monotonically worse, up to 1.5926 at scale 1.0). `_cold_start_projection` therefore still uses
only `next_fixture_difficulty`, completely unchanged — a deliberate, evidence-based decision NOT
to ship the Elo-based alternative there, not an oversight. `next_fixture_elo_differential` is
still persisted and still reaches `MODEL_TRAINING`'s real feature matrix automatically (see
`app.modeling.candidates`'s generic column iteration) — a trained gradient-boosted model can
learn its own weight for the signal, unlike this one hand-written formula's single fixed
coefficient, so the negative result is scoped to this specific formula, not a verdict on Elo's
value as a signal overall. This evaluation covered one real season (narrower than the 6-season
evidence behind the cold-start blend's other constants) out of respect for ClubElo's own "be
reasonable" rate-limit ask — documented honestly rather than overstated.

**Defensive-contribution reliability — real infrastructure, currently zero real evidence, deliberately not
wired into production.** A real gap surfaced through direct manual comparison of two real defenders at
the same price who project similarly on aggregate history alone (Tarkowski, Everton: hits FPL's real
2025-26+ CBIT bonus threshold in 57.9% of real games; Calafiori, Arsenal: 0.0%) — the cold-start blend's
`career_avg_early_season_points_prior_seasons` input is an AGGREGATE historical points average that
can't distinguish a repeatable defensive-work-rate-driven scoring pattern from a more variance-prone
one, even though the real, current CBIT/CBIRT bonus (verified 2026-08-18 against real Premier League/
Draft Fantasy sources, not assumed: defenders need 10 combined clearances+blocks+interceptions+tackles;
midfielders/forwards need a higher 12, since ball recoveries also count for them) is part of the actual
points formula being predicted.

`app/modeling/cold_start_validation.py` adds a fifth, DEF/MID-only observation set and coefficient
sweep for this: `build_dc_reliability_observations` (same leakage-safe `career_avg_early_season_points_
prior_seasons`/`filter_to_strictly_prior` machinery every other builder in that module reuses, further
requiring a real, non-null `defensive_contribution` gameweek in STRICTLY PRIOR seasons) and
`sweep_dc_reliability_coefficient_candidates` (`career_avg_baseline * (1 + (prior_dc_hit_rate - 0.5) *
coefficient)` — centered on a 0.5 coin-flip hit rate, mirroring the fixture-difficulty adjustment's own
"neutral at difficulty 3" structure). Persists to five new `cold_start_validation_runs` columns
(migration 0042: `n_dc_reliability_observations`, `dc_reliability_sweep_json`,
`default_dc_reliability_coefficient`/`_mae`, `best_dc_reliability_coefficient`/`_mae`,
`dc_reliability_caveat`), wired into `COLD_START_PROJECTION_VALIDATION`'s `run()` with the same
never-fails-the-main-sweeps discipline as the true-rookie/price-prior population.

**Real, honest result (checked directly against the real dev database, not assumed): this currently,
always, evaluates ZERO observations.** `defensive_contribution` is only non-null starting the 2025-26
season (the CBIT rule's first real season) — leakage-safe validation needs a player's STRICTLY PRIOR
season's own DC data to predict a LATER, already-known season's outcome, and 2025-26 has no
strictly-prior season carrying this stat; the next season that would (2026-27) hasn't been played yet.
**This signal is therefore NOT wired into `_cold_start_projection` at all** — there is no real evidence
to justify even the SIGN of an adjustment, let alone a magnitude, and this codebase's own established
discipline (see the three constants and the Elo result above) is evidence-first, not intuition-first.
Real evidence becomes possible automatically, with no code changes needed, the first time 2026-27
gameweeks are ingested with their own real `defensive_contribution` values — re-run
`COLD_START_PROJECTION_VALIDATION` at that point.

Instead, the same real signal (`defensive_contribution_hit_rate_prior_seasons`) is exposed as a
PURELY INFORMATIONAL field on `GET /players/{id}/projection` — `defensive_contribution_hit_rate_
prior_seasons`/`_prior_games` (DEF/MID only, `None` for GKP/FWD or a player with no such gameweek) —
so a manager can see the same real historical reliability signal directly rather than hand-querying
`player_gameweek_stats.defensive_contribution`, without the API pretending it already shapes
`projected_points`.

Both `SQUAD_OPTIMIZATION` and `/optimize/preview` rank candidates by
points summed across the next `SQUAD_BUILD_HORIZON_LENGTH` (6) still-actionable
gameweeks (`app.optimization.gameweek_resolution.resolve_horizon_gameweeks`
+ `app.transfers.horizon_projection.compute_horizon_points` — the same
horizon machinery `TRANSFER_OPTIMIZATION` already used), not just the
immediate next gameweek — a fresh pre-season squad is a multi-week
commitment, so ranking should reflect each player's run of fixtures, not
a single gameweek snapshot. (`LINEUP_OPTIMIZATION` deliberately stays
single-gameweek — picking a starting XI for one specific gameweek is a
different question.) Candidate `projection_source` is labeled
`horizon_{N}gw_{champion_family}` when a champion model exists, or
`horizon_{N}gw_heuristic` when it doesn't (no champion ever
promoted/selected).

**`/optimize/preview`'s composition-risk signals and exclude-and-resolve**
(added post-review, after a live example: a preview squad spent GBP15.5m —
15.5% of the whole budget — on one striker; a manual re-solve with that
player removed from candidates found an alternative squad scoring only
~1.3 points less over the 6-gameweek horizon, but with a real structural
weakness — a single genuine starting forward with near-zero-projected bench
cover — that `solve_joint_squad_and_xi`'s pure expected-points-maximization
objective has no way to see or flag). Two things close that gap:

- `app/optimization/squad_risk.py` (pure, no I/O) computes, over every
  solved squad regardless of request parameters: `compute_budget_concentration`
  (the single most expensive squad member's cost as a % of the total
  budget, flagged `is_notably_concentrated` once it exceeds 1.5x an
  evenly-spread per-member share — relative to the constraints in force,
  not a hardcoded FPL-specific percentage) and `compute_bench_depth_warnings`
  (human-readable warnings for positions with exactly ONE starter whose
  best bench cover projects below 25% of that starter's own points, or has
  no bench cover at all — positions with 2+ starters are deliberately never
  flagged, since losing one starter among several is a normal, already-
  priced-in risk, not a structural weakness). Surfaced on every
  `SquadPreviewOut.composition_risk`, computed automatically, not opt-in.
- `POST /optimize/preview`'s optional body, `SquadPreviewRequest.exclude_player_ids`,
  filters those players out of the candidate pool before solving — the
  exact comparison above, now a real, repeatable request instead of a
  one-off manual Python-shell re-solve. `SquadPreviewOut.excluded_player_ids`
  echoes the request back so a caller can label the comparison. Excluding
  candidates down to infeasibility (e.g. every player at one position)
  degrades gracefully to `solver_status="infeasible"` with empty players/
  cost/composition_risk fields — never a 500.

**`GET /players/{player_id}/projection`'s "why this projection" breakdown**
(added post-review): the response includes `is_cold_start_season`,
`is_cold_start_player`, `ep_next`, `career_avg_points_prior_seasons`,
`career_avg_early_season_points_prior_seasons`, `next_fixture_difficulty`,
`champion_model_family`/`champion_model_version_id`,
`availability_risk_reason`/`availability_risk_severity` (see "Availability
risk notes" below), and a one-line `explanation` string. These are
display-only surfacing of numbers already computed by
`project_player_with_champion`/`_cold_start_projection` and a best-effort
extra `compute_features_for_player_gameweek` call for display purposes —
not a new explainability system (no SHAP, no per-feature attribution).
`champion_model_family` is set whenever `projected_points` came from a live
champion-model prediction, checked via `.startswith(f"ml_champion_
{family}")` rather than `==` (the source string may carry the
`_availability_risk_noted` suffix on top — see below) — during a cold-start
season, a champion may exist and be the reason `_cold_start_projection`
engaged at all, but the fields shown are the blend's own inputs, not the
(unused, untrustworthy-for-this-input-shape) champion prediction, so
`champion_model_family` correctly stays `None` in that case. `is_cold_start_season`
and `is_cold_start_player` are two distinct booleans computed independently
(via `app.optimization.projection.is_cold_start_season`/`is_cold_start_player`,
both thin public wrappers around this module's own per-season/per-player
private checks — see "The cold-start check is per-player, not per-season"
above): `is_cold_start_season` True always implies `is_cold_start_player`
True (zero rows for everyone means zero for this one player too), but not
the reverse — a player can be individually cold-start (a late debut, a long
injury absence) in a season that has otherwise moved on. `_explain_projection`
takes `is_cold_start_season` as an explicit parameter so its one-line text
can distinguish the two ("First gameweek of a new season..." vs. "No
current-season data yet for this player...") rather than always claiming
it's the season's own first gameweek, which would be misleading for a
mid-season individually-cold-start player. The explanation text correctly
names `career_avg_early_season_points_prior_seasons` (not the stale
full-season number) whenever `_cold_start_projection` actually used it —
fixed in the same session `career_avg_early_season_points_prior_seasons`
was added. See `apps.api.routers.players._explain_base_projection`/
`_explain_projection` for the exact source-string-to-sentence mapping (the
latter strips `app.optimization.projection.AVAILABILITY_RISK_NOTED_SUFFIX`
before delegating to the former, then appends a risk-note sentence).

**Availability risk notes** (migration `0031`) close a gap neither FPL's
own official data nor this codebase's models can see: a player who isn't
officially listed injured/doubtful (`players.chance_of_playing_next_round`/
`news`, refreshed automatically every `DATA_INGESTION` run and already
down-weighted by `project_player`'s own `_AVAILABILITY_SCALE_THRESHOLD`
multiplier) but has a real, sourced, judgment-based reason to expect
reduced early-season involvement — concretely, several Premier League
players returning from a deep 2026 FIFA World Cup run with a compressed
pre-season. **This is NOT a background job that checks the news itself —
there is no autonomous news-monitoring anywhere in this system** (it only
ever talks to the official FPL API); a `player_availability_risk_notes`
row is added by a human, or an agent doing fresh research on request, the
same way a manager makes any other judgment call about their own squad.

- `packages/database/models/player_availability_risk_note.py`: each row is
  an independent note (NOT the single-active-row-per-invariant pattern
  `seasons.is_current`/`model_versions.is_champion`/`leagues.is_priority`
  use elsewhere in this schema) — `reason` (text), `severity` (a
  projection multiplier in (0, 1]), `valid_from_fpl_event_id`/
  `valid_until_fpl_event_id` (the gameweek NUMBER window it applies to,
  inclusive both ends), `created_by` (free text — this app has no user
  table, only a single shared bearer token). A note simply stops applying
  once its own gameweek window passes; no explicit deactivation needed.
- `app.optimization.projection.get_active_availability_risk_note`
  (public) / `_apply_availability_risk_notes` (private): looked up as the
  FINAL step of `project_player_with_champion`, after whichever branch
  (champion-model, cold-start blend, or plain `project_player` fallback)
  produced its own result — so a flagged risk applies regardless of
  whether a champion model happened to be consulted. Stacks
  multiplicatively with (does not replace) the existing
  `chance_of_playing_next_round`-based scaling — two independent,
  legitimate reasons a projection might be too high. If more than one
  note is active for the same player/gameweek, the MOST severe (lowest
  multiplier) wins. Appends `AVAILABILITY_RISK_NOTED_SUFFIX` to
  `PlayerProjection.source`, which every source-string-matching caller
  (`_explain_projection`, the `champion_model_family` check above) must
  strip/account for.
- CRUD via `apps/api/routers/players.py`: `GET`/`POST`/`DELETE
  /players/{player_id}/availability-risk-notes[/{note_id}]`. `POST`
  validates `valid_from_fpl_event_id <= valid_until_fpl_event_id` (422 if
  backwards). Minimal frontend on the player detail page
  (`apps/web/components/availability-risk-notes.tsx`): a danger-styled
  banner on the projection card when a note is active for the target
  gameweek, a list of every note ever recorded (past and present, for
  audit), and an inline add/remove form — no separate admin page.
- Seeded (via the real API, on the real dev database, not a raw SQL
  insert) with two real, sourced notes as of 2026-07-25: Declan Rice and
  Bukayo Saka (both Arsenal/England, lost the World Cup semifinal July 15,
  confirmed not returning to training until Aug 10 — 11 days before the
  Aug 21 opener), severity 0.65, valid GW1-3. Three Arsenal players who
  actually won the tournament for Spain (Raya, Zubimendi, Merino) were
  deliberately NOT flagged — limited World Cup minutes, expected back in
  good shape.

**Role notes** (migration `0032`) close a completely different gap, found
while reading real FPL scout/tips coverage: who takes penalties/corners/
free-kicks is a real signal that directly affects a player's expected
points, but it is not exposed anywhere in FPL's own `bootstrap-static` API
and is not reliably inferable from historical scoring alone (a role can
change hands mid-season, or a new signing can be handed set-piece duty on
arrival before it ever shows up in their own scoring history).

**Deliberately a separate table/mechanism from availability risk notes,
not a reuse of it**, for two reasons:

1. **Direction**: risk notes are always a discount (severity in (0, 1]);
   role notes are normally a boost (severity > 1.0, though nothing at the
   DB level hard-enforces that — the multiplication works identically
   either way). A player could plausibly have an active risk note (a minor
   knock) AND an active role-boost note (just handed penalties)
   SIMULTANEOUSLY — both are real, independent facts, and both should
   apply, multiplied together, not have one silently overwrite the other.
2. **Multi-note tie-break direction is inverted**:
   `get_active_availability_risk_note` picks the MOST severe (lowest
   multiplier) when several risk notes overlap — understating a real risk
   is worse than overstating it. `get_active_role_boost_note` picks the
   HIGHEST severity (strongest boost) when several role notes overlap —
   the underlying fact ("who currently takes penalties") is usually
   unambiguous once properly sourced, so the strongest/most specific
   confirmed claim is the more sensible default.

- `packages/database/models/player_role_note.py`: same shape as
  `player_availability_risk_note` (`role` is an extra free-text column,
  e.g. `"penalties"`/`"corners"`/`"free_kicks"` — not an enum, nothing
  branches on the exact string) — same independent-note,
  no-explicit-deactivation pattern. Role assignments tend to be
  longer-lived than an injury recovery window, so `valid_until_fpl_event_id`
  left `NULL` (open-ended) is the common case in practice, unlike risk
  notes which usually set both ends.
- `app.optimization.projection.get_active_role_boost_note` (public) /
  `_apply_role_boost_notes` (private): called strictly AFTER
  `_apply_availability_risk_notes` in `project_player_with_champion`
  (multiplication is commutative, so order doesn't change the math, but
  fixes the order suffixes appear in `PlayerProjection.source` —
  `AVAILABILITY_RISK_NOTED_SUFFIX` then `ROLE_BOOSTED_SUFFIX` when both
  apply). `apps.api.routers.players._explain_projection` strips both
  suffixes (role first, since it's appended last) before delegating to
  `_explain_base_projection`, then appends a sentence per suffix actually
  present.
- CRUD via `apps/api/routers/players.py`: `GET`/`POST`/`DELETE
  /players/{player_id}/role-notes[/{note_id}]`, identical shape to the
  availability-risk-notes endpoints. Minimal frontend
  (`apps/web/components/role-notes.tsx`): a success-green-styled banner
  (deliberately NOT the risk notes' danger-red styling — this is good
  news, not bad news) on the projection card when a note is active, plus
  the same list-everything/inline-add-remove pattern.
- Seeded (via the real API, on the real dev database) with two real,
  sourced notes as of 2026-08-14: Pascal Groß (Brighton — note the FPL
  `web_name` uses the German eszett, not "Gross"; confirmed as Brighton's
  penalty/corner/free-kick taker per Premier League Scout coverage) and
  Igor Thiago (Brentford, confirmed keeping penalty duties after a
  22-goal season), both severity 1.15, `valid_from_fpl_event_id=1`,
  open-ended.

**`GET /players/notes/active`** (`apps/api/routers/players.py`) — added to
support Scout Watch's dedup logic (see that section below), the one
player-notes endpoint that is NOT scoped to a single player. Returns every
currently-active row from BOTH tables, across every player, as a flat list
(`ActiveNoteOut`: `player_id`, `web_name`, `category`
[`"availability_risk"` | `"role_boost"`], `role`, `reason`, `severity`,
`valid_from_fpl_event_id`, `valid_until_fpl_event_id`, `created_at`,
`created_by`). "Active" mirrors `get_active_availability_risk_note`/
`get_active_role_boost_note`'s own window check
(`_is_note_active` — `(valid_from is None or fpl_event_id >= valid_from) and
(valid_until is None or fpl_event_id <= valid_until)`) without the
single-player scoping those functions have, resolved against the current
season's next unfinished gameweek (`_resolve_relevant_fpl_event_id`, the
same "lowest `fpl_event_id` with `finished=False`" pattern
`GET /gameweeks/next-deadline` uses — deliberately independent of the
optimization gate). Returns `[]`, not a 404, when there's no current season
or no unfinished gameweek (e.g. between seasons) — an empty list is a valid,
expected answer, not an error. Covered by
`tests/unit/test_players_router_active_notes.py` (pure `_is_note_active`
boundary cases) and `tests/integration/test_api_active_notes.py` (empty/
missing-season/missing-gameweek/expired/not-yet-started boundary cases,
then both note categories together).

**Note-severity rubric + outcome tracking** (Milestone 7 extension, added
post-review): every real availability-risk/role-boost note created this
season was a one-off, subjective judgment call with no consistent framework
and no way to check afterward whether it was well-calibrated — unlike the
cold-start blend's own half-life/fixture-difficulty/blend-ratio constants
(backtestable against 6 real historical seasons), a specific injury or role
change has no repeated sample to backtest against. Two additive pieces close
this gap without pretending a single note can be validated in isolation:

- **The rubric** lives as a docstring directly on
  `apps.api.schemas.players.AvailabilityRiskNoteCreate`/`RoleNoteCreate` (not
  a separate markdown doc — matches this file's own "docstrings carry real
  documentation" precedent used throughout this codebase, and keeps the
  guidance next to the exact schema every note-creation path — manual, Scout
  Watch's automated proposals, a future session — actually goes through).
  Grounds four severity bands per note type in the real notes already
  created (Kroupi.Jr/Minteh/Madjo at 0.05-0.10 for confirmed multi-week
  absences; Senesi/Rice/Saka/Guehi at 0.60-0.65 for significant World-Cup
  fatigue risk; Rogers/Lacroix/Woltemade at 0.75-0.85 for milder fatigue or
  genuine rotation competition; Szoboszlai at 1.08 for a confirmed narrower
  sub-role; Groß/Igor Thiago at 1.15 for confirmed primary set-piece duty;
  Joao Pedro at 1.35 for a verified quantitative blend-underweighting
  pattern; Osula at 1.6 for a confirmed major backup-to-starter role
  upgrade), cited as worked examples so future severity choices have real
  precedent to point at instead of picking a number in isolation.
- **The feedback loop** extends `GAMEWEEK_REVIEW` (Layer 2/3's own existing
  pattern, not a separate system) via `app.learning.note_effectiveness`:
  for every reviewed gameweek, `evaluate_note_effectiveness_for_outcome`
  finds every `optimization_run_players` row in the recommended squad whose
  `projection_source` carries `AVAILABILITY_RISK_NOTED_SUFFIX`/
  `ROLE_BOOSTED_SUFFIX` (an immutable fact about what applied at generation
  time), re-resolves that note's own severity via
  `get_active_availability_risk_note`/`get_active_role_boost_note` (safe
  since notes are never edited in place, only created/deleted), and
  reconstructs the without-note projection by DIVIDING OUT that note's own
  multiplier from the already-persisted with-note projection — exact and
  forensically correct (uses the same number the recommendation was actually
  generated from), not a fresh, independently-recomputed projection that
  could drift from what was actually decided on. Compares both projections'
  absolute error against the real `player_gameweek_stats` outcome, recording
  `moved_closer_to_actual` (`True`/`False`/`None` on an exact tie). A player
  with BOTH an active risk note and an active role note stacked at once
  produces TWO rows (one per note type, each isolating just its own
  multiplier's contribution) via a unique constraint on
  `(recommendation_outcome_id, player_id, note_type)`.
  - **`note_effectiveness_evaluations`** (migration `0033`): insert-only
    child of one `recommendation_outcomes` row (`ON DELETE CASCADE`, mirrors
    `counterfactual_results`/`failure_attributions`' own run-child
    convention — delete-then-reinsert on a rerun, never updated in place).
    `availability_risk_note_id`/`role_boost_note_id` are `ON DELETE SET
    NULL` (not `CASCADE`) — both note tables ARE deletable via their own
    `DELETE` endpoints, and this row's own severity/points/error columns
    already captured everything needed to stay meaningful as a historical
    record after the source note itself is later removed.
  - Wired into `GameweekReviewJobRunner` as `_track_note_effectiveness`, the
    SAME failure-isolation shape as `_maybe_recalibrate_price_signal`
    (never raises — a tracking failure folds into
    `summary_json.note_effectiveness_error` and is logged, but can never
    flip `learning_reports.run_status` to `FAILED`, since this is additive
    information layered on top of GAMEWEEK_REVIEW's own already-succeeded
    primary work). Surfaced in `report_markdown`'s new "Note effectiveness"
    section (`app.learning.report._note_effectiveness_table`), in
    `summary_json` (`note_effectiveness_count`/
    `note_effectiveness_negative_count`/`note_effectiveness_error`), and as
    a `notable_findings_json` entry (`compose_notable_findings`'s new
    `note_evaluations` parameter) ONLY when `moved_closer_to_actual is
    False` — a note that worked as intended, or landed on an exact tie,
    isn't material enough to surface, mirroring this module's own existing
    "notify only on material findings" principle.
  - No automated recalibration is built on top of this (deliberately
    scoped out — `price_signal_calibrations`' own real recalibration
    mechanism is the closest precedent for what a MATURE version of this
    could eventually become, once enough real (severity band, outcome)
    pairs accumulate across real gameweeks to say something statistically
    meaningful — nowhere close to that pre-season). This can only be
    verified against synthetic/seeded data right now
    (`tests/integration/test_note_effectiveness.py`,
    `tests/integration/test_gameweek_review_job.py`'s own "Note-
    effectiveness outcome tracking wiring" section) since the season hasn't
    started and no real gameweek has been reviewed with an active note in
    play yet.

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
  - `goal_involvement_overperformance_5gw` (migration 0035) — a
    regression-to-mean signal absent from every other column here (all
    points/minutes history): `(goals + assists) - (expected_goals +
    expected_assists)`, averaged over the most recent 5 gameweeks that
    actually carry xG data (`app.features.engineering
    .rolling_goal_involvement_overperformance`). xG/xA are schema-drift-null
    for every 2020-21/2021-22 `player_gameweek_stats` row and fully populated
    from 2022-23 onward (verified against real ingested data) — a gameweek
    missing xG is excluded from the window, never zero-filled.
  - `defensive_contribution_5gw` (migration 0035) — a rolling average of
    FPL's own real 2025-26+ CBIT threshold-bonus stat
    (`app.features.engineering.rolling_defensive_contribution`) — genuine
    points-scoring data, not just an external stat, previously ingested into
    `player_gameweek_stats.defensive_contribution` but never used as a
    feature anywhere. Schema-drift-null before 2025-26 (the season this FPL
    rule was introduced), fully populated for 2025-26.
  - `gameweek_schedule_gap_days` (migration 0036) — a GAMEWEEK-level (not
    player-specific) structural signal: days since the previous gameweek's
    own `deadline_time`, in the same season
    (`app.features.engineering.gameweek_schedule_gap_days`). Deliberately
    distinct from the existing player-specific `rest_days` column — `rest_days`
    is ambiguous between an individual player being rested/rotated (a neutral
    signal) and the whole league pausing at once for an international
    window/major tournament (a real fatigue risk, but only for players who
    were actually away on duty); this feature disambiguates by being the same
    value for every player in a given gameweek. Systematizes what this
    session otherwise did entirely by hand (manually-typed World Cup fatigue
    notes for Rice/Saka/Guéhi/Senesi/Lacroix/Rogers, all GW1-3 2026-27) —
    generalized to any real scheduling disruption, not just a World Cup
    specifically, with zero new external data dependency (purely derived from
    already-ingested `gameweeks.deadline_time`). Real, verified effect
    (checked 2026-08-17 against all 6 real historical seasons in this app's
    own database): a `>=12`-day gap reliably separates genuine disruptions
    (international windows consistently ~14 days; the real Qatar World Cup
    mid-season pause in 2022-23 shows 44 days) from ordinary in-season
    variation (max 11 days outside real disruptions). Players who started
    (`>=60` minutes) the gameweek immediately before such a gap averaged 2.836
    points / 69.5 minutes in the gameweek immediately after it, versus 3.541
    points / 85.8 minutes for the general population of all `>=60`-minute
    appearances (n=5597 vs n=47291). Stores the raw day-count, not a
    pre-thresholded boolean, so the trained model can learn its own
    dose-response rather than a hand-picked cutoff being baked into the data.
    `NULL` for a season's own gameweek 1 (no previous gameweek to compare
    against — a different, already-handled cold-start signal).
  - All three are purely additive columns — `FEATURE_VERSION` was NOT bumped
    (matches the precedent set by `career_avg_early_season_points_prior_seasons`,
    migration 0030, also additive-only): existing rows just show `NULL` for
    these columns until `FEATURE_GENERATION` is re-run for that season,
    read correctly as "unknown", not "zero". `app.modeling.candidates
    ._feature_record` iterates `PlayerFeatures.__table__.columns` generically,
    so all three reach the training matrix automatically with no candidates.py
    changes needed — this was previously true of `expected_goals` too, except
    no function ever populated it (see `app/features/engineering.py`'s
    module docstring history) — the real gap this closes was "ingested but
    never computed as a feature", not "computed but not wired to the model".
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
- **`START_PROBABILITY_MODEL_TRAINING`** (`app/jobs/handlers
  /start_probability_training.py`, `app/modeling/start_probability.py`): a
  SEPARATE, additive auxiliary classifier — P(player starts this gameweek,
  proxied as `minutes >= 60`, summed across a Double Gameweek's fixture
  rows) — trained via the same walk-forward fold machinery as
  `MODEL_TRAINING` (`app.modeling.walk_forward`), but never touching
  `model_versions`/the champion at all. Persists to its own
  `start_probability_models` table (migration 0037), a curated feature
  subset (`FEATURE_COLUMNS`: rolling minutes/points history, rest days,
  position — deliberately excludes price/ownership/fixture difficulty, see
  that module's own docstring), and a `HistGradientBoostingClassifier` for
  the same NaN-native reason `MODEL_TRAINING`'s own gradient-boosting
  candidate is chosen. **`is_active` defaults to `False` and is never
  flipped by the training job itself** — activating a row to actually
  influence live projections is a deliberate, human-reviewed decision
  (direct DB update, no `POST .../activate` endpoint exists), based on that
  run's own real, walk-forward-backtested `aggregate_auc`/
  `aggregate_accuracy`/`aggregate_f1`/`aggregate_log_loss`. `POST
  /models/start-probability-training` triggers a run; `GET
  /models/start-probability[/{id}]` reads persisted rows.
  When (and only when) a row IS activated,
  `app.optimization.start_probability_loader.load_active_start_probability_model`
  loads it, and `project_player_with_champion` accepts it as an optional
  `start_probability_model` parameter (default `None` — every pre-existing
  caller is completely unaffected): if provided, it scales whatever
  projection the champion/cold-start path already produced by the
  predicted P(start), appended as a `_start_probability_adjusted` source
  suffix, applied BEFORE the `player_availability_risk_notes`/
  `player_role_notes` multiplier stack so a manually-authored note can
  still further adjust a model-informed projection. Built specifically to
  replace what was previously only doable by hand — manually-typed
  availability-risk-note severities reasoned from prose scouting reports
  (e.g. real notes this session for Woltemade/Osula) — with a learned
  signal from the same rolling-minutes history already computed for the
  main model.
- **Promotion gates** (`app/modeling/promotion.py`, Milestone 7 Layer 5): a
  newly-trained challenger only becomes champion after clearing gates
  evaluated against real `model_challenger_evaluations` rows
  (`app.modeling.shadow_evaluation.compute_shadow_challenger_evaluation`,
  called from `GAMEWEEK_REVIEW` once a gameweek finishes). Three gates are
  real: `minimum_sample_size` (>= 6 gameweek evaluations before any verdict),
  `beats_champion_by_margin` (challenger's mean MAE beats champion's by >=5%),
  and `per_position_segmentation` — an architecture-level diagnostic (not a
  per-challenger recomputation): does the CURRENT single-global-model design
  have a real, meaningful per-position blind spot a position-specific
  approach would fix? Backed by a new job, `POSITION_SEGMENTATION_VALIDATION`
  (`app/modeling/position_segmentation.py` +
  `app/jobs/handlers/position_segmentation_validation.py`), which fits a
  GLOBAL `HistGradientBoostingRegressor` and 4 POSITION-SPECIFIC ones
  (GKP/DEF/MID/FWD) per real walk-forward fold and compares real per-position
  MAE — reusing `app.modeling.walk_forward`'s exact fold-building, never
  touching `model_versions`/`is_champion` (the position-specific models are
  fit fresh, in memory, purely to compute this diagnostic; never serialized,
  never promotion-eligible themselves). Persists to its own
  `position_segmentation_validation_runs` table (migration `0038`); absence
  of a run never blocks a promotion (informational, like
  `historical_backtest_replay`, the one gate still genuinely unimplemented).
  **Re-run 2026-08-17 after fixing the GK/GKP mislabeling bug** (see "Known
  real gaps" below) against this session's 6 real historical seasons
  (2020-21 through 2025-26, `feature_version=1`, n=151,412 total
  observations, GKP now correctly covering 16,901 real goalkeeper rows
  instead of the old, mislabeling-truncated 1,968): DEF (-1.0%), MID (-1.4%),
  FWD (-1.2%), and now GKP too (-3.1%, `global_mae=0.658` vs
  `position_specific_mae=0.679`) ALL show the global model already at least
  as good as a position-specific split (`mae_gap_pct` negative every time —
  no meaningful blind spot anywhere, GKP included). This supersedes the
  original run's untrustworthy GKP number (a spurious +35% artifact of the
  mislabeling bug's near-total zero-inflation on a tiny unrepresentative
  slice) with a real, consistent, trustworthy result. **No production
  constant or the live champion was changed as a result of this
  evaluation** — real, honest evidence that the existing single-global-model
  architecture doesn't have a demonstrated case for splitting by position,
  now confirmed across all 4 positions rather than 3 of 4.
- **`app/learning`** (Milestone 7, "recursive learning"): after a
  recommendation's outcome is known, `outcome_ingestion.py` records actuals,
  `counterfactuals.py` compares the followed recommendation against
  alternatives that weren't taken, `attribution.py` classifies *why* a miss
  happened (`FailureReasonCode`/`FailureCategory`), `signals.py` computes
  rolling-window signals (`LearningSignalType`, e.g.
  `ROLLING_PROJECTION_MAE`), `note_effectiveness.py` tracks whether an
  active availability-risk/role-boost note moved a projection closer to or
  further from the real outcome (see "Note-severity rubric + outcome
  tracking" above). `GAMEWEEK_REVIEW` ties this all together and,
  on a **sustained** `ROLLING_PROJECTION_MAE` breach, triggers `MODEL_TRAINING`
  in-process (not via a separate scheduled workflow) plus a promotion-gate
  check — see `gameweek_review.py`'s own docstring for the exact
  failure-isolation boundary (a retraining failure never fails the review
  itself).
- **Price-signal auto-recalibration** (same failure-isolation boundary,
  independent step) — every `GAMEWEEK_REVIEW` run also calls
  `GameweekReviewJobRunner._maybe_recalibrate_price_signal`, which grid-
  searches `app.players.price_signal_calibration.calibrate_thresholds`
  against every real (signal, observed-outcome) pair accumulated so far for
  the season, and — only once at least `MIN_CALIBRATION_SAMPLE_SIZE=30`
  pairs exist — inserts a new `price_signal_calibrations` row (`is_active`
  flipped exactly like `is_current`/`is_champion`/`is_priority` elsewhere in
  this schema) that `GET /players/{id}` then uses instead of
  `price_signal.py`'s hardcoded `DEFAULT_*` thresholds. This can't run
  before now because there was previously no historical record of what the
  transfer-momentum signal looked like at any past point in time —
  `players.transfers_in_event`/`transfers_out_event`/`selected_by_percent`
  are overwritten every `DATA_INGESTION` run — so migration `0029` adds
  `player_price_signal_observations`, an insert-only snapshot of those
  fields appended for every current-season player at the end of every
  successful `DATA_INGESTION` run (`app.ingestion.normalization
  .record_price_signal_observations`). `app.players.price_signal_calibration
  .build_calibration_samples` pairs each player's consecutive observations
  into a sample, labeled `price_changed=True` iff a real `player_price_history`
  row falls strictly between them — this is genuinely unusable until a live
  season has run long enough to accumulate both enough observations AND at
  least a few real FPL price changes, which is exactly the "once real
  gameweek data starts flowing" trigger this was built for.

## Data model — key relationships

48 ORM models under `packages/database/models/`. The load-bearing shape:

- **`seasons`** is the root scope for almost everything (`players`, `teams`,
  `positions`, `gameweeks`, `fixtures`, `rules_config`, `player_gameweek_stats`,
  `player_features`, `manager_squad_snapshots`, `optimization_runs`,
  `transfer_optimization_runs`, `chip_optimization_runs`, `rival_profiles`,
  `recommendation_audits`, `learning_signals`, `learning_reports`,
  `model_challenger_evaluations`, `player_price_history`,
  `player_price_signal_observations` all carry a direct `season_id` FK).
  Exactly one `seasons` row should have `is_current=True` at a time —
  nothing enforces this at the DB level, it's an application-level
  invariant.
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
- **`price_signal_calibrations`** is also deliberately **not** season-scoped
  (global, no FK at all — just `ratio_threshold`/`absolute_floor`/
  `sample_size`/`accuracy`/`is_active`/`calibrated_at`) — mirrors
  `model_versions`' own reasoning: the price-momentum heuristic itself isn't
  a per-season concept, and every recalibration INSERTS a new row rather
  than updating one in place (same "never overwrite, always append and
  re-point the active flag" philosophy as `model_versions`/
  `optimization_runs`), preserving a full audit trail of how the thresholds
  drifted over time. See "Price-signal auto-recalibration" above.
- **`leagues.is_priority`** (added post-review, migration `0027`): a manager
  can sync more than one real H2H league (`POST /leagues/sync` has no cap),
  but the Rivals page needs exactly one to show by default — same
  application-enforced "exactly one `True` at a time, unset every other row
  first" invariant as `seasons.is_current`/`model_versions.is_champion`. The
  first league ever synced becomes priority automatically
  (`app.jobs.handlers.league_sync._ensure_a_priority_league_exists`); every
  one after that stays non-priority until `POST /leagues/{league_id}/priority`
  is called explicitly. The Rivals page is a tab switcher (`?league=<id>`
  search param, Server Component, same pattern as `/players`'s own
  filter/pagination state) defaulting to whichever league has the flag.
- **Squad differentials** (added post-review): `GET /leagues/{league_id}/rivals`
  also returns, per rival, `players_only_you_have`/`players_only_rival_has` --
  the set-difference between the calling manager's own current squad
  (`manager_squad_snapshots` row with `is_current=True`) and that rival's
  last AVAILABLE picks snapshot (`RivalProfile.last_known_snapshot_id` ->
  `rival_gameweek_snapshots.squad_player_ids`), resolved to `web_name`/
  position. Computed by the pure `app.h2h.differential.compute_squad_differential`
  (no I/O, unit-tested directly against `tests/unit/test_squad_differential.py`)
  and batched into exactly 3 extra queries total regardless of rival count
  (current squad, rival snapshots, player name/position lookup) --
  `apps.api.routers.leagues.list_league_rivals` never queries per-rival.
  Both fields are `null` together (never partially populated) when either
  side is unresolvable: no current squad synced yet, or this rival has no
  AVAILABLE picks snapshot. Rendered in `apps/web/components/squad-differential.tsx`
  as two columns ("Only you have" / "Only they have") inside each rival's
  card on the Rivals page.
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
- **`seasons.total_players`** (migration `0028`) — bootstrap-static's own
  top-level `total_players` (total registered FPL manager accounts),
  refreshed on every `DATA_INGESTION` run by
  `app.ingestion.service.get_or_create_current_season`. Exists solely to
  convert a player's `selected_by_percent` into an estimated absolute
  owner count (2% of 100k managers vs. 2% of 11m are very different
  signals) — see the next bullet.
- **`app.players.price_signal`** (migration `0028` adds
  `players.transfers_in_event`/`transfers_out_event`/`cost_change_event`,
  captured from bootstrap-static's per-player, per-gameweek transfer
  counts — FPL resets both to 0 at the start of every gameweek, so this is
  a "since the last price update" signal, distinct from
  `player_gameweek_stats.transfers_in`/`transfers_out`, which are
  season-cumulative totals from the historical/`element-summary`
  pipeline). `classify_price_momentum` is a deliberately simple,
  explainable heuristic — **not** FPL's real (undisclosed) price-change
  algorithm — that flags `likely_rise`/`likely_fall` when net transfer
  activity crosses both a ratio threshold (`DEFAULT_MOMENTUM_RATIO_THRESHOLD`
  = 2% of the player's own estimated owner base) and an absolute floor
  (`DEFAULT_MIN_ABSOLUTE_NET_TRANSFERS` = 1000 net transfers, to avoid false
  positives on tiny-owned differentials where a handful of transfers is a
  huge percentage of a tiny owner base). Both are overridable per-call
  (`ratio_threshold`/`absolute_floor` params) — `GET /players/{id}`'s router
  looks up the current active `price_signal_calibrations` row and passes its
  tuned values in when one exists, falling back to the hardcoded defaults
  otherwise; see "Price-signal auto-recalibration" below for how that row
  gets populated. Computed in the router, never a stored column, on three
  responses: `PlayerDetail.price_momentum` (`GET /players/{id}`),
  `PlayerSummary.price_momentum` (`GET /players` — the active-calibration
  lookup is hoisted to once per request, not once per row), and
  `OptimizationRunPlayerOut.price_momentum` on `POST /optimize/preview`
  specifically (a live, "right now" signal, deliberately left `None` on
  every *persisted* run — `SQUAD_OPTIMIZATION`/`LINEUP_OPTIMIZATION`'s own
  responses never populate it, since a past run being re-fetched shouldn't
  silently show today's momentum for yesterday's decision). Shown as a
  compact badge wherever a manager is actually choosing/scanning players
  (players list, squad preview) and the fuller version on the player
  detail page; `unknown` (the common pre-season case, since
  `transfers_in_event`/`transfers_out_event` are typically 0/0 before real
  gameweeks start) is suppressed in the compact spots rather than shown as
  a badge with nothing to say.
- **`player_availability_risk_notes`** (migration `0031`) — curated,
  judgment-based fitness/availability risk notes, distinct from the
  official `chance_of_playing_next_round`-driven signal above; see
  "Availability risk notes" under "The optimization gate" above for the
  full rationale and how `project_player_with_champion` applies them.
- **`player_role_notes`** (migration `0032`) — curated, sourced set-piece
  (penalty/corner/free-kick) duty notes; a projection BOOST, deliberately
  a separate table/mechanism from `player_availability_risk_notes` (a
  discount) rather than a reuse of it — see "Role notes" under "The
  optimization gate" above for the full "why two mechanisms" rationale.
- **`note_effectiveness_evaluations`** (migration `0033`) — insert-only
  child of `recommendation_outcomes` (`ON DELETE CASCADE`), one row per
  (outcome, player, note type) recording whether an active availability-
  risk/role-boost note moved a recommended-squad projection closer to or
  further from the real outcome; see "Note-severity rubric + outcome
  tracking" under "The optimization gate" above for the full rationale.

## API (`apps/api`)

Single FastAPI app, single shared-secret auth (`apps/api/auth.py`,
`require_shared_token`, `secrets.compare_digest`, fails closed with 401) on
every router except `health.py`. No user table — deliberate, this is a
single-manager system. CORS via `Settings.cors_allowed_origins_list`
(empty/fail-closed by default). Routers: `players`, `gameweeks`, `optimize`
(squad/lineup/transfers, plus `chips` sharing the same `/optimize` prefix —
watch route registration order in `main.py`, the catch-all `GET
/optimize/{id}` must be registered after every static sibling path or it
swallows them; within `chips.py` itself, `GET /optimize/chips/windows` is
likewise registered before `GET /optimize/chips/{chip_optimization_run_id}`
for the identical reason), `managers`/`squads`, `h2h`, `recommendations`,
`jobs`/`learning`, `leagues`, `models`. List endpoints are capped-at-200,
most-recent-first (no real pagination) except `players`, which has real
page/page_size.

Two read-only endpoints exist specifically because they're genuinely usable
*pre-season*, unlike almost everything else gated behind
`check_optimization_gate` or requiring a synced/manual squad — both read
already-ingested data, never touch the gate, and were added so a manager
has something actionable to look at before FPL marks a gameweek live:
- **`GET /gameweeks/next-deadline`** — the next unfinished gameweek's
  `deadline_time` plus the REAL `check_optimization_gate` verdict for it
  (`gate_allowed`/`gate_reason_code`, not an approximation derived from
  `is_current` alone). 404s as `NO_CURRENT_SEASON`/`NO_UPCOMING_GAMEWEEK`
  rather than fabricating a stale date. Powers the Dashboard's deadline
  countdown card, shown in every Dashboard state (including "no
  recommendation yet") since the real FPL deadline matters regardless of
  what this system's own gate currently permits.
- **`GET /optimize/chips/windows`** — this season's chip-scheduling windows
  (`app.chips.windows.resolve_chip_windows`, wildcard/free hit/bench
  boost/triple captain, each with a `start_event`/`stop_event` gameweek
  range) sourced from the current season's `rules_config.raw_chips_json`
  (already-ingested by `DATA_INGESTION`'s own `RULES_SYNC` step — never a
  fresh live FPL API call at request time). FPL publishes these before a
  ball is kicked, so unlike a real `CHIP_SIMULATION` run (needs a squad,
  passes through the gate) this is available immediately. Each window's
  `status` (`past`/`open`/`upcoming`) is computed against the season's
  current gameweek — every window is `upcoming` pre-season, when there is
  none. Powers the `/chips` overview page (distinct from `/chips/[runId]`,
  a specific simulation run's results).

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
see README's exact command). Pages (15 total): Dashboard (+ deadline
countdown card, every state), Team (+ manual squad create/duplicate/
delete), Players (list w/ price-momentum column + detail w/ live
projection), Chips (read-only chip-window overview — see the API section
above), Rivals (+ H2H simulate), History (all 4 run types), Learning
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

- `tests/unit/` (51 files): no DB, no network.
- `tests/integration/` (55 files): real Postgres, monkeypatched FPL client(s)
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

## On-demand data refresh (`POST /data/refresh`)

`DATA_INGESTION` previously had no HTTP trigger at all (CLI/`make ingest`
and the scheduled dispatcher only). That became a real, live problem:
`scheduled-dispatcher.yml`'s cron only ever writes to an ephemeral,
throwaway Postgres spun up fresh for each CI run (see its own comment —
"this is still NOT the real scheduling logic") and has never once touched
the actual database backing a real deployment of this app, because this
app has never been deployed anywhere with a persistent, internet-reachable
database — only a local `docker compose` stack exists. Confirmed live:
the local dev database sat 19 days stale mid-transfer-window and missed
23 real summer signings/transfers, with nothing to catch it short of
manually running a script.

`POST /data/refresh` is the fix that doesn't require deploying anywhere:
a manager triggers a fresh `DATA_INGESTION` run themselves, right before
checking their squad, via a button on the Team page. Synchronous, exactly
like every other job-triggering endpoint in this codebase — no job-id
polling. This still relies on someone actually clicking it; see "League
Watch" below for the parallel, harder constraint (an *automatic*, session-
independent check needs either a persistent hosted database this app's
local stack doesn't have, or — for read-only checks against a public API
that don't need this app's own database at all — a standalone workflow
like League Watch).

## Local scheduler (`scripts/scheduler.py`, `docker-compose.yml`'s `scheduler` service)

Real gap this closes, found 2026-08-18: `scheduled-dispatcher.yml`/`ingest.yml` (the two
GitHub Actions workflows that could theoretically automate `DATA_INGESTION`) each spin up their
OWN throwaway, empty Postgres service container inside the GitHub Actions runner — they have
NEVER touched the real local database this repo's own `docker-compose.yml` `postgres` service
serves, and structurally CANNOT (GitHub's runners have no network path to a user's own machine).
Before this, every real job run against the real database, this whole project long, happened
because a human manually ran `scripts/run_job.py` or hit an API endpoint — there was no automation
at all for the real thing, only for CI self-tests against ephemeral databases. `league-watch.yml`/
`scout-watch.yml` (below) are the only genuinely-automated things that existed before this, and
they deliberately avoid the problem by never touching the database at all.

The real scheduling logic lives in `app.scheduling.tick` (`run_tick`, plus its pure/testable
decision functions — `gameweeks_needing_review`, `is_interval_due`,
`event_ids_missing_authoritative_recommendation` — see `tests/unit/test_scheduling_tick.py`),
deliberately kept importable (not buried in a script) since it now has TWO real callers — see
"Cloud deployment" below for the second one. `scripts/scheduler.py` is a thin wrapper around it: a
long-running loop, started as its own `scheduler` service in `docker-compose.yml` (reuses the
`api` service's own image/Dockerfile — nothing extra to build), that wakes up every 15 minutes and
calls `run_tick()`. Each real tick, checked against real state pulled from the database, triggers:

- **`RECOMMENDATION_GENERATION` → `GAMEWEEK_REVIEW`** — the moment a gameweek is both
  `Gameweek.finished=True` and has no `learning_reports` row yet (checked via a real join, not
  guessed), exactly once per gameweek, never re-fired once a report exists (`learning_reports` is
  insert-only per run, so a naive "run every tick" would silently create a duplicate report
  forever). Real gap found and closed 2026-08-18: `GAMEWEEK_REVIEW` hard-refuses
  (`NO_AUTHORITATIVE_RECOMMENDATION`) without an existing `is_authoritative=True`
  `recommendation_audits` row for the target gameweek, and nothing previously created one
  automatically — this scheduler now checks for that first (`_fetch_authoritative_recommendation_event_ids`)
  and calls `RECOMMENDATION_GENERATION` with an explicit `target_fpl_event_id` (so it needs no
  manager-squad-snapshot/current-season resolution of its own) before attempting the review.
  `RECOMMENDATION_GENERATION` is a pure, best-effort correlator, not a hard-precondition gate — it
  degrades to a real but LOW-confidence audit (see its own module docstring's `compute_confidence`)
  when the 4 optimizer/simulation components it looks for have no run for this gameweek (e.g.
  pre-deadline, no manager squad ever synced for it — an expected state this whole project has hit
  repeatedly), rather than failing outright, so it still produces a real, reviewable row. If it
  somehow still fails, `GAMEWEEK_REVIEW` simply fails its own `NO_AUTHORITATIVE_RECOMMENDATION`
  check right after (logged, not fatal to the loop) and both are retried next tick, since the
  gameweek correctly remains "not reviewed yet". Verified end-to-end against an isolated scratch
  database: a seeded finished-but-unreviewed gameweek correctly triggered
  `RECOMMENDATION_GENERATION` then `GAMEWEEK_REVIEW` (both `SUCCEEDED`) on the first tick, and a
  second tick correctly fired neither again.
- **`LEAGUE_SYNC`** — every 6 hours (real rival picks only change post-deadline and don't shift
  intra-week once known, so this is more than sufficient cadence without wastefully re-hitting
  FPL's public league API every 15-minute tick).
- **`DATA_INGESTION`** — every 4 hours, mirroring `scheduled-dispatcher.yml`'s own reasoned
  cadence for its CI self-test of the same job, applied here to the real thing.

Deliberately does NOT trigger `MODEL_TRAINING`/anything promotion-adjacent directly —
`GAMEWEEK_REVIEW` already has its own internal trigger for that on a sustained MAE breach; a
second path here would just double-fire it. Calls `app.jobs.service.execute_job` directly
(`TriggerType.LOCAL`, `requested_by="scheduler"` — NOT `TriggerType.GITHUB_SCHEDULE`, since this
genuinely never runs via GitHub Actions, and using that value would misleadingly imply the
opposite of the whole point of this component), same pattern as `scripts/run_job.py`, rather than
looping HTTP calls to its own API — avoids an unnecessary network hop and avoids managing/rotating
an auth token inside the container. Each job run is wrapped so a failure (a transient FPL API
blip, a genuine bug) is logged and never crashes the loop or blocks the next tick's other checks
(mirrors `GameweekReviewJobRunner._maybe_recalibrate_price_signal`'s own established
failure-isolation precedent).

**Complements, does not replace**, the existing manual "sync"/"review" buttons already in
`apps/web` (`rivals/page.tsx` → `POST /leagues/sync`, `learning/page.tsx` →
`POST /jobs/review-gameweek`) — those stay for on-demand, right-now triggering; the scheduler is
the automatic path for when nobody's watching. Starts by default with a plain `docker compose up`
(`docker compose stop scheduler` opts out); only runs while the host machine/Docker stack is
actually up (see `docs/operations.md` for that tradeoff) — **as of 2026-08-21, no longer the only
option**: see "Cloud deployment" immediately below for a real, free-tier alternative that works
even when the local machine is off.

### Cloud deployment (`POST /internal/scheduler-tick`, `render.yaml`, `docs/deployment.md`)

Real gap this closes: the local scheduler above only runs while the user's own machine is on --
useful for local development, not for reaching a live dashboard from a phone with the laptop
closed. Real research into free-tier cloud hosting (see `docs/deployment.md`'s own "why this
specific combination" section for the concrete providers/tradeoffs found) surfaced a real
constraint: none of the realistic free options offer a persistent background-worker process the
way `docker compose`'s own `scheduler` service does -- a free web service only runs in response to
real HTTP traffic.

`apps/api/routers/internal.py` adds `POST /internal/scheduler-tick` specifically for this: an
authenticated (same `dependencies=[protected]` wiring every other router gets in `apps.api.main`)
endpoint that runs exactly one real tick, synchronously, by calling `app.scheduling.tick.run_tick`
directly -- the EXACT same function `scripts/scheduler.py`'s own local loop calls, not a
duplicated reimplementation. A free external cron service (cron-job.org, see `docs/deployment.md`
for the concrete setup) hits this endpoint every 15 minutes once deployed, functionally replacing
the local loop.

This only works correctly because `run_tick` was already designed to be fully stateless between
calls -- see `app.scheduling.tick`'s own module docstring: every "was it time yet" check derives
fresh from the real `jobs` table's own existing audit trail (`_fetch_last_successful_run_at`), not
an in-memory dict. An HTTP request/response cycle has no memory of the previous one, so this
statelessness (originally just a "don't duplicate derivable state" design choice) turned out to be
exactly what made the HTTP-triggered path correct with zero extra work -- verified end-to-end with
a real, temporary local server: a first real tick fired `DATA_INGESTION` (real success against
FPL's live API), and a second, fully independent HTTP call five seconds later correctly did NOT
re-fire it, proving the real database-derived state persisted correctly across two separate
requests with no shared process memory between them.

`render.yaml` (repo root) is a real Render Blueprint deploying the existing `Dockerfile` as-is (no
separate deployment-specific build). Deliberately does NOT provision a Render-managed Postgres --
real research found Render's own free Postgres expires 30+14 days after creation, incompatible
with a season-long tool; Supabase's free Postgres (auto-pauses after 7 days of inactivity, data
retained, resumes on next request) is the real, more durable choice instead. `docs/deployment.md`
has the full, real, ordered setup checklist (Supabase → Render → Vercel → cron-job.org) with every
real environment variable each service needs.

Database migrations against the real Supabase database run via the `Dockerfile`'s own `CMD`
(`uv run alembic upgrade head && exec uv run uvicorn ...`), not Render's `preDeployCommand` --
confirmed via a real deploy (2026-08-21) that `preDeployCommand` does not run on Render's free
plan (a deployed API hit a live `psycopg.errors.UndefinedTable: relation "model_versions" does
not exist` against a schemaless Supabase database), and the Shell tab documented as a fallback
turned out to also be a paid-tier-only feature. Running the migration on every container start
(idempotent, a no-op once the schema is current) is the one mechanism that's actually guaranteed
on the free plan, including after free-tier cold-start wake-ups. Local `docker-compose.yml`
overrides `command:` for the `api`/`scheduler` services, so this default `CMD` never runs there
and local dev is unaffected.

## Proactive push alerting (Milestone 9 Layers 1-2: `push_subscriptions`, `app/notifications/`, `apps/api/routers/push.py`, `app.scheduling.tick`'s failure alerts)

Real gap this closes: nothing in this system ever proactively told the manager anything — every
real event (a job failure, an approaching deadline, a price/injury change) was only visible if
someone opened the dashboard and went looking. Caught live (2026-08-23): the real cloud
`DATA_INGESTION` job had been silently failing on every scheduled run for hours before anyone
noticed.

Web Push via an installable PWA, not a native app or SMS — free, no phone number, works via "Add
to Home Screen" on the existing dashboard. This is a single-shared-secret system with **no
per-user or per-device identity** (see `apps/web/lib/session.ts`'s own docstring: every session
cookie is interchangeable, no session store), so `push_subscriptions` is a flat list of this
manager's own devices, keyed purely by each subscription's own `endpoint` URL — every real alert
goes to every subscribed device via `app.notifications.push_sender.send_push_to_all`, there is no
per-user routing to build.

**Backend**: `pywebpush`/`py-vapid` (new dependencies). `Settings.vapid_private_key` is the
**full real PEM text** of a real VAPID keypair (`uv run vapid --gen`, see `docs/deployment.md`) —
`app.notifications.push_sender` reconstructs a `py_vapid.Vapid02` object via `Vapid02.from_pem(...)`
explicitly at send time, deliberately not relying on `pywebpush.webpush()`'s own ambiguous
string-format guessing (file path vs. raw vs. DER) for a real secret. The config default is
deliberately an **invalid placeholder**, not a usable dev keypair like `APP_SHARED_TOKEN`'s own
default — a real PEM private key must never be committed to source regardless of how "throwaway"
it is (gitleaks correctly flagged an earlier attempt at a committed dev keypair as a real leak), so
push fails closed with a clear `ValueError` until a real key is configured via env var. Tests that
need `send_push_to_all` to run without crashing mock `py_vapid.Vapid02` at the boundary instead
(see `tests/integration/test_api_push.py`'s `_StubVapid`) — PEM parsing is `py_vapid`'s own concern,
not this codebase's business logic to verify.
`GET /push/vapid-public-key` / `POST /push/subscribe` (upsert by `endpoint`) /
`DELETE /push/subscribe` (idempotent) / `POST /push/test` (sends a real push to every subscription
right now — the concrete end-to-end verification step) live in `apps/api/routers/push.py`, same
auth wiring as every other router. Per-subscription failure isolation in `send_push_to_all`
(mirrors `LEAGUE_SYNC`'s own per-rival isolation): a subscription the push service reports `410
Gone` is deleted; any other failure is logged and skipped, never blocking the rest of a batch.

**Frontend**: `apps/web/public/manifest.json`/`sw.js`/`icons/` (a real, minimal service worker —
`push` → `showNotification`, `notificationclick` → focus/open the app; no offline caching, this
app reads live, frequently-changing data). `components/push-notification-toggle.tsx` (wired into
`components/nav.tsx`) requests permission, subscribes via `pushManager.subscribe()`, and POSTs the
subscription through the existing `/api/proxy` pattern — the raw VAPID public key is fetched at
runtime via `GET /push/vapid-public-key`, never build-time-baked, matching `APP_SHARED_TOKEN`'s
own "Render env vars are the real source of truth" precedent.

Two real bugs found and fixed while building this:
- **`apps/web/proxy.ts`'s auth-gate matcher blocked `/manifest.json`/`/sw.js`/`/icons/*`** —
  caught live (2026-08-23) via a real browser check: both returned this middleware's `307`
  redirect to `/login` instead of the real files, which would have made every service worker
  registration fail outright (a service worker script must be served with the right content type,
  not a login-page redirect). Fixed by adding them to the matcher's exclusion list, alongside the
  existing `_next/static`/`_next/image`/`favicon.ico` exemptions.
- **`apps/web/Dockerfile`'s `output: "standalone"` build never copied `public/` into the runner
  stage** — a well-known Next.js standalone gotcha (unlike `.next/static`, `public/` isn't copied
  automatically) that was invisible before this milestone since `apps/web/public/` didn't exist
  yet. Fixed with an explicit `COPY --from=builder /app/public ./public`.

Verified locally via a real Playwright-driven Chromium session (not just `next build`): real
login, real service worker registration (confirmed `registered: true` via
`navigator.serviceWorker.getRegistration()`), real `GET /push/vapid-public-key` fetch, and a real
`pushManager.subscribe()` attempt that got as far as Chromium's actual push-service handshake
before failing with `AbortError: Registration failed - permission denied` — a well-documented
Headless Shell limitation (no real push-service backend reachable in that sandboxed environment),
not a defect in this code. Full end-to-end delivery to a real device is verified via `POST
/push/test` against the live Render deployment instead (see `docs/deployment.md`).

**Layer 2 — job-failure alerts**: `app.scheduling.tick._run_job_safely` calls
`_alert_on_job_failure` right after any job reaches `JobStatus.FAILED`. `_previous_job_of_type_failed`
looks at the single most recent OTHER job of the same `JobType` (by `created_at`, real `jobs` table,
scoped by `job_type` only — not `requested_by`, so a human's own manual retry of the same job type
still counts as the same incident) — a push fires only when that previous job did **not** also fail,
so a real multi-hour failure streak (like the real `DATA_INGESTION` incident that motivated this
whole milestone) sends exactly one notification, not one every tick. Fully isolated from job
outcome: `_alert_on_job_failure` has its own internal `try/except` (no VAPID key configured, the
push service being down, etc. is logged as `scheduler.job_failure_alert_error` and swallowed,
never allowed to make an already-failed job look like it also broke the scheduler tick itself).
Covered by `tests/integration/test_job_failure_alerts.py`, which mocks `send_push_to_all` at
`app.scheduling.tick`'s own imported reference (not `py_vapid`/`pywebpush` — that boundary is
`test_api_push.py`'s job) and deliberately seeds a controlled "previous job" row before each test,
since the streak query's real, shared, never-truncated Postgres means leftover `DATA_INGESTION`
history from other tests in the same suite run would otherwise make the test order-dependent.

## League Watch (`.github/workflows/league-watch.yml`)

A standalone, deliberately non-generic scheduled workflow, distinct from
every other job/workflow in this repo: it does not spin up the app's own
Postgres, run any `JobType` handler, or touch this app's database at all.
It hits FPL's own public, unauthenticated `leagues-h2h/{id}/standings/`
endpoint directly (cron `17 9 * * 1,5`, plus `workflow_dispatch`) and
compares the response against `.github/state/league-814682-watch.json`
(committed back to the repo only when something actually changes) for two
specific transitions: a new manager joining the league, or `standings`
going from empty to populated (FPL only ever populates real H2H standings
once the season has started and at least one gameweek has been played —
a newly created league sits in an empty-standings "new entries" pending
state indefinitely before that). On a real change, it opens a GitHub
issue (label `league-watch`) via `gh issue create`; on no change, it
exits silently — it does not spam an issue every run.

This exists because a specific user asked for an automatic,
session-independent check on their own real FPL H2H league (814682,
"Pro Clubs '27") — hardcoded to that one league/manager rather than
built as a reusable app feature, unlike every other workflow in this
repo. It was built as the durable alternative to a `CronCreate`-scheduled
in-session check (session-only, dies when the chat session closes,
auto-expires after 7 days) once the user asked for something that
survives closing the session. If this stops being needed (e.g. once the
league has real standings and the user is checking the app's own Rivals
page directly), it can simply be deleted — it has no dependency from
anything else in this codebase and nothing depends on it.

## FPL Scout Watch (`.github/workflows/scout-watch.yml`)

A second standalone scheduled workflow, same shape as League Watch but a
genuinely different mechanism underneath: twice a week (cron `22 8 * * 1`
Monday morning for an early-week roundup, `47 21 * * 4` Thursday night for
a pre-deadline check — most FPL gameweek deadlines fall Friday evening,
occasionally Saturday, and the most decision-relevant news, press
conferences and final fitness updates, lands Wed-Fri; a Monday-only run
would miss exactly that — plus `workflow_dispatch`), it calls the
**Gemini API's** Google Search grounding tool (not Anthropic's) to research
real, current FPL "scout"/tips coverage (Premier League's own Scout,
Fantasy Football Scout, and similar credible outlets) for two things this
app's own data can't source on its own: judgment-based availability risk
FPL's official status hasn't caught yet, and confirmed set-piece-duty
changes (who takes penalties/corners/free-kicks — genuinely absent from
`bootstrap-static`, see the "Set-piece duty" section above).

Cost, measured from a real run's actual token usage (not estimated): well
under $0.01/run — 4 grounded searches, ~2,760 tokens total, all within
Gemini 2.5 Flash's free daily grounding allowance (1,500 requests/day) at
this call volume. Getting there required real, live debugging (see the
workflow's own git history/comments for the full trail) that corrected an
earlier, too-optimistic claim in this doc: Google requires *new* AI Studio
accounts (a policy change as of March 2026) onto a Prepaid billing plan
before grounding will actually work at all, even though base
(non-grounded) calls succeed under a separate free allowance — a genuinely
free, no-card AI Studio tier does still exist, but not automatically for a
brand-new project. A minimum ~$10 prepaid balance (one-time; credits last
a year) was required to unblock this project specifically, alongside two
unrelated real fixes: the Gemini API needing to be explicitly enabled on
the underlying Cloud project (a standard one-time Cloud Console step), and
the docs' example model name (`gemini-3.6-flash`) turning out not to carry
the same free grounding quota as the one that actually worked
(`gemini-flash-latest`).

**Research + propose only — never applies anything directly**, for the
same structural reason as the on-demand data refresh and League Watch:
this app has never been deployed anywhere with a public,
GitHub-Actions-reachable database, so even if this workflow wanted to
`POST` a new `player_availability_risk_notes`/`player_role_notes` row
itself, it has no way to reach the app that would receive it. Every run
opens a GitHub issue (label `fpl-scout-digest`) with the model's findings,
formatted as ready-to-apply proposals (player, category, suggested
severity, suggested gameweek range, sourced reason, source URLs) — a
human (or an agent, in a later session) reviews and applies whatever's
genuinely useful via the real `POST /players/{id}/availability-risk-notes`
/ `POST /players/{id}/role-notes` endpoints, exactly the same way every
note seeded so far this season was created.

**TWO sequential Gemini calls, not one** — added after a real, live run of
this exact workflow confidently reported that Arsenal's William Saliba
"underwent back surgery," when independent sources actually confirm
surgery was explicitly RULED OUT; that error was only caught by manual
human review, which is exactly the failure mode this workflow exists to
reduce. Call 1 ("research pass") is unchanged — the model researches and
drafts findings in the same delimited block format as before. Call 2
("verification pass") hands those DRAFT findings back to a fresh model
call, same endpoint/model/`google_search` grounding tool, with an explicit
instruction to independently re-search and mark each finding
`CONFIRMED`/`CORRECTED`/`RETRACTED` — not rubber-stamp the draft. The
GitHub issue body is built from the SECOND pass's output only; a corrected
finding shows the fix inline via `CORRECTION_NOTE` rather than printing
both the draft and corrected versions side by side. Still just two calls
per run, not an elaborate multi-round debate — cost for the pair stays in
the same well-under-$0.01 range as the original single call (see below).

**Squad-aware and duplicate-aware**: the verification-pass prompt also
embeds two committed state files, read via a `Checkout repo` step added
for exactly this (the original single-call version didn't need repo
contents at all — everything went through `curl`/`gh`):

- `.github/state/current-squad.json` — the user's real decided starting
  XI, captain, and bench, seeded verbatim (Roefs; Gabriel/Alderete/
  Lacroix/Virgil; Semenyo/Mbeumo/Stach/Zubimendi;
  Haaland(C)/Woltemade; bench Button/Destan/Mukasa/D.Essugo). Unlike
  `league-814682-watch.json`, there is no live FPL API this workflow can
  poll to auto-sync a squad (that needs the manager's own team endpoint,
  which needs auth this workflow doesn't have), so this file is manually
  maintained — its own `_comment` field says so, and says to treat
  `starting_xi`/`captain` as higher-confidence than `bench` (the note
  records that Kroupi was already swapped for Rogers once this season,
  and the bench may have shifted slightly since `last_updated_at`). Every
  `CONFIRMED`/`CORRECTED` finding for a player in this file is prepended
  `SQUAD_FLAG: IN YOUR SQUAD` in the issue.
- `.github/state/known-notes-snapshot.json` — a snapshot of every
  currently-active note the real app already knows about, generated (not
  hand-written) from the real dev database via the new
  `GET /players/notes/active` endpoint (see the availability/role-notes
  section above) using `scripts/export_notes_snapshot.py --base-url
  <api> --token <APP_SHARED_TOKEN>` (mirrors `scripts/run_job.py`'s CLI
  style; `httpx`-based, already a project dependency). As of the session
  that built this, a real export captured 12 active notes (Rice, Saka,
  Kroupi, Rogers, Guehi, Senesi, Lacroix, Minteh, Madjo — availability
  risk; Groß, Igor Thiago, Szoboszlai — role boost). Needs the same
  periodic manual re-run/re-commit as the squad file — no automatic sync
  either. A finding that duplicates an already-active note (same player,
  category, overlapping range, comparable severity/role) gets
  `DUPLICATE_FLAG: ALREADY KNOWN` plus a `CORRECTION_NOTE` explaining
  what's already there, instead of being re-proposed as new; a finding
  that meaningfully updates/contradicts an existing note stays flagged as
  new but says in `CORRECTION_NOTE` that it supersedes the old one.

Unlike League Watch, this does **not** diff against previous state and
stay silent on "no change" — natural-language news content has no clean
automatable "nothing changed" signal the way a structured JSON API
response does, so every scheduled run opens a fresh digest issue rather
than guessing whether last week's findings are still current. The prompt
itself instructs the model to say "no new finding this week" rather than
fabricate something plausible-sounding, mirroring this repo's own
established "accuracy over always having something to report" discipline
(see the availability-risk-notes seeding sessions this was modeled on).

Requires a `GEMINI_API_KEY` repository secret (a free-tier key created at
aistudio.google.com — **not** Google Cloud/Vertex AI, a different product
that always requires billing); the job fails loudly, not silently, if the
secret is missing.

## Post-deadline real-data verification (`scripts/post_deadline_smoke_test.py`)

Six features have only ever run against `tests/`'s committed fixtures/
synthetic seasons: `MANAGER_SYNC`'s squad fetch, chip-inventory derivation
(`app.chips.inventory`), `LEAGUE_SYNC`'s standings/matches sync, rival
profile derivation (`app.h2h.rival_state`), rival squad differentials
(`app.h2h.differential`), and price-signal calibration
(`app.players.price_signal_calibration`) — each gated on a real,
FPL-platform-imposed precondition (a gameweek deadline passing, a season
starting, a gameweek finishing, or real price movements accumulating) that
a synthetic fixture can't reproduce. `scripts/post_deadline_smoke_test.py`
runs all six against the real dev database and the real FPL API, and
classifies each as `PASS` (precondition met, real output verified),
`PENDING` (precondition genuinely not met yet — expected, not a failure),
or `FAIL` (precondition met and it still broke — the only outcome that
should fail CI/block a release). Full runbook: `docs/operations.md`'s "Post-
deadline real-data verification" section.

Live-run while building this script (2026-08-17, pre-season) surfaced a real
platform behavior not previously documented anywhere in this codebase: FPL's
`leagues-h2h/{id}/standings/` genuinely returns zero `standings.results` for
a real, active league (814682, "Pro Clubs '27" — 3 joined managers
confirmed live under `new_entries`) until the season has started AND at
least one gameweek has finished — the same shape of precondition
`league-watch.yml` was independently built to poll for, but not previously
called out in `app/jobs/handlers/league_sync.py`'s own docstring, which now
documents it directly.

## Known real gaps (as of this doc)

- `PROJECTION_GENERATION` job type: enum member only, no handler, deliberately -- no defined
  consumer/table for it to write to (`project_player`/`project_player_with_champion` are
  already called directly by every job that needs a projection, computed live, never cached).
- No official-API season-aggregate history (`element-summary/{id}/history_past`) ingestion — moot in practice since `HISTORICAL_DATA_INGESTION` already provides strictly richer (gameweek-level, not just season-total) historical data from a different source.
- **Resolved migration collision (2026-08-17):** revision id `0033` was independently claimed by
  two concurrent, unrelated pieces of work built in separate worktrees — `note_effectiveness_evaluations`
  and `cold_start_validation_runs` — both declaring `down_revision = "0032"`. Reconciled at merge
  time: `note_effectiveness_evaluations` (merged into `main` first) kept revision `0033`;
  `cold_start_validation_runs` was renumbered to `0034` with `down_revision = "0033"`, restoring a
  single linear chain. `alembic upgrade head` re-run against both the real dev database and
  `fpl_decision_engine_test` after the renumber. Three more concurrent agents' migrations that same
  night were reconciled the same way: `goal_involvement`/`defensive_contribution` -> `0035`,
  `gameweek_schedule_gap_days` -> `0036`, `start_probability_models` -> `0037`,
  `position_segmentation_validation_runs` -> `0038`.
- **Resolved data-quality issue (found and fixed 2026-08-17): goalkeeper position labels were
  inconsistent in `player_gameweek_stats.position_short_name_at_time`/`player_features
  .position_short_name`.** Root cause: `gws/merged_gw.csv`'s own free-text `"position"` column
  uses `"GK"` for goalkeepers, inconsistently — the SAME real goalkeeper (e.g. Altay Bayindir,
  Håkon Valdimarsson) was labeled `"GK"` in some gameweeks and `"GKP"` in others, within the same
  season, across all 6 real historical seasons. `app.modeling.candidates.KNOWN_POSITIONS = ("GKP",
  "DEF", "MID", "FWD")` only recognizes `"GKP"`, so every `"GK"`-labeled row (the large majority —
  e.g. 2023-24: 3406 `"GK"` vs only 394 `"GKP"`) was silently one-hot-encoded as "unknown
  position", not goalkeeper — affecting every model ever trained on this historical data,
  including the live champion, not just the `POSITION_SEGMENTATION_VALIDATION` job that surfaced
  it. Fixed in `app.ingestion.historical_normalization._normalize_position_short_name` (aliases
  `"GK"` → `"GKP"` at ingestion time; the live ingestion path was never affected — it derives
  `position_short_name_at_time` from the `Position` table, already canonical). `HISTORICAL_DATA
  _INGESTION` (`season_label=all`) and `FEATURE_GENERATION` (`season_label=all`) were re-run
  against the real dev database to backfill every existing row (upsert-safe, keyed on
  `player_id`/`gameweek_id`/`fixture_id`) — verified via `information_schema` query: zero `"GK"`
  rows remain, `"GKP"` counts now equal the old `"GK"` + `"GKP"` totals exactly (e.g. 2023-24:
  3800). Two smaller stray labels (`"AM"`/`"MNG"`, 2024-25 only, 320/440 rows) were investigated
  and confirmed NOT a bug — real Assistant Manager/Manager fantasy picks (verified against real
  ingested names: Guardiola, Arteta, Postecoglou, ...), correctly excluded from `KNOWN_POSITIONS`
  since they aren't real player positions. The live champion has NOT yet been retrained on the
  corrected data — see the `MODEL_TRAINING`/promotion sections above for why a fresh training run
  is safe to do (creates a `SHADOW_CHALLENGER`, never auto-promotes) but the decision whether to
  do so, and whether to act on it, is left to explicit human review.
- **Resolved, real bug (found and fixed 2026-08-17): `jobs.job_type`/`status`/`trigger_type` were
  silently too narrow on the real dev database.** These are `native_enum=False` `VARCHAR` columns
  whose real width in an already-migrated database is frozen at whatever the enum looked like the
  FIRST TIME migration `0001` ran there — not automatically kept in sync with later additions
  (see `0006`'s own precedent for this exact bug class, which widened `ingestion_runs.run_type`
  but explicitly reasoned `jobs.job_type` didn't need it *at the time*). Three job types added
  this session (`start_probability_model_training`/`cold_start_projection_validation`/
  `position_segmentation_validation`, 32 chars each) exceeded the real dev database's frozen
  `VARCHAR(25)` `job_type` column — every real invocation failed with a DB-level truncation
  error. Invisible to every integration test run this session, since a fresh test database
  created via `alembic upgrade head` auto-sizes the column against the CURRENT enum and never
  exhibits this bug. Fixed via migration `0041` (widens all three `jobs` columns to `VARCHAR(50)`)
  plus a new regression test (`tests/unit/test_job_enums.py`) asserting every enum member fits
  the real column width — fails loudly in CI the moment a future addition would exceed it, rather
  than silently succeeding in tests while failing in production.
- **Resolved, full schema-wide audit (2026-08-18) of the same risk class.** The `jobs`-table fix
  above was explicitly left as "not a full schema-wide audit" — this closes that gap for real.
  Audited every `native_enum=False` `SAEnum(...)` column across `packages/database/models/*.py`
  (46 total) against the REAL dev database's actual `information_schema.columns` widths (not
  recomputed from the current Python enum state alone, which only reflects what a FRESH database
  would create). **39 of 46 had ZERO real headroom** — their real column width exactly equalled
  their enum's current longest member, the identical "just got lucky so far" state
  `jobs.job_type` was silently sitting in before it actually broke. Fixed via migration `0043`
  (widens all 39 to `VARCHAR(64)`; the remaining 7 — `chip_type` x2, `ingestion_runs.run_type`,
  `optimization_runs.recommendation_type`, and the 3 `jobs` columns already widened by `0041` —
  already carried real headroom and were left untouched). Applied to both `.env.test` and the
  real dev database.

  The permanent fix is a new, fully generalized regression test
  (`tests/unit/test_enum_column_widths.py`) that supersedes `test_job_enums.py`'s narrower,
  `jobs`-specific version (kept as-is, not removed): it dynamically discovers EVERY
  `native_enum=False` column across the whole ORM metadata (so it automatically covers any
  future new table/column, not just today's 46) and requires each one to have an explicit,
  consciously-chosen width recorded in a maintained registry — a column found without one fails
  the test immediately, forcing whoever adds a new enum-backed column to pick and record a real
  width rather than silently trusting Alembic's auto-computed default. This is the actual,
  durable resolution to a bug class that has now surfaced three separate times
  (`ingestion_runs.run_type` in `0006`, `jobs.*` in `0041`, these 39 in `0043`) — always invisible
  in CI (fresh test database every run), always real against the persistent dev database.
- **Resolved, real bug (found and fixed 2026-08-22): the entire recommendation engine targeted
  the wrong gameweek for hours after every real deadline, on the just-deployed cloud dashboard.**
  `app.optimization.gameweek_resolution`'s private helper (then named
  `_unfinished_gameweek_ids_ordered`) resolved "what gameweek should I plan for" by filtering on
  `Gameweek.finished.is_(False)` — but `finished` only flips to `True` once every match in that
  gameweek has actually been *played*, days after its deadline. Confirmed live: GW1's real
  deadline passed (`2026-08-21 17:30 UTC`) while `finished` was still `False` (matches still being
  played that weekend), and the dashboard generated a "GW1 recommendation" the manager could no
  longer act on. The bug was systemic, not a one-off: `resolve_target_gameweek` and
  `resolve_horizon_gameweeks` (both built on that one helper, plus `resolve_gameweeks_within_window`
  for chip scheduling) feed squad optimization, lineup optimization, transfer optimization, chip
  simulation, H2H simulation, recommendation generation, `GET /gameweeks/next-deadline`, and the
  `/players/notes/active`/live-projection-preview endpoints — every one of which independently
  duplicated the same `finished=False` inline query in two of those API routes rather than sharing
  the resolver at all. Fixed by renaming the helper to `_actionable_gameweek_ids_ordered` and
  changing its filter to `Gameweek.deadline_time > now` (a gameweek's deadline always precedes it
  finishing, so this is sufficient on its own — no separate `finished` check needed), with an
  explicit `now: datetime` parameter threaded through every public function (no default — forces
  every caller to be deliberate; mirrors `app.scheduling.tick.is_interval_due`'s existing
  testable-time-based-logic precedent, since this codebase has no Clock/time-provider abstraction).
  `resolve_most_recent_finished_gameweek_event` (used by `H2H_SIMULATION`'s documented off-season
  fallback — see the SEPARATE entry below for why `LEAGUE_SYNC` stopped using it the very next day)
  was deliberately left unchanged here — it's genuinely asking a retrospective "what already
  happened" question, not part of this bug. The two duplicated inline queries (`gameweeks.py`'s
  `next-deadline` endpoint
  and `players.py`'s `_resolve_relevant_fpl_event_id`) were refactored to call the shared resolver
  instead of hand-rolling the same query a third and fourth time. Uncovered a second, real bug
  while fixing test fixtures: `tests/integration/conftest.py`'s `unique_bootstrap_json` shifts
  every event's deadline to the SAME synthetic year for season-label collision-avoidance — correct
  for "still actionable" fixtures, but any fixture meant to represent "already finished, off-season"
  gameweeks needs a genuinely PAST deadline now, not just `finished=True`. The fix's own first
  attempt (a day-offset counter) introduced a THIRD real bug — consecutive draws rarely cross a
  year boundary, so different tests silently derived the same season label and merged their data
  via get-or-create semantics (caught by `test_rival_profile.py` asserting `profile_count == 3`
  and getting `8`) — replaced with a dedicated year counter (`_past_deadline_year_counter`,
  disjoint from both real season labels and the existing future-synthetic pool), mirroring
  `_season_year_counter`'s own exact collision-avoidance guarantee.
- **Resolved, real bug (found and fixed 2026-08-23): `LEAGUE_SYNC` never attempted to fetch a
  rival's real picks for hours-to-days after every real deadline, even though they were already
  available.** Caught live by the manager directly: told the app "the matches are out, check" for
  their real GW1 opponent (Samazing B2B), and a direct curl against FPL's own
  `entry/{id}/event/1/picks/` confirmed real, full picks data (points, captain, full squad)
  already existed — while `LEAGUE_SYNC` had synced zero rival snapshots. Root cause:
  `_sync_one_rival` resolved which gameweek's picks to fetch via
  `resolve_most_recent_finished_gameweek_event` (`finished=True`-gated), the exact same
  finished-vs-deadline conflation the previous day's fix (above) had already found and fixed
  everywhere else — this one call site was missed because it's a genuinely different, third query
  shape (`LEAGUE_SYNC` wants the most recent gameweek whose deadline has passed, not the next
  actionable one and not the most recent settled one). Fixed by adding
  `resolve_most_recent_deadline_passed_gameweek_event` (deadline-based, alongside — not replacing —
  `resolve_most_recent_finished_gameweek_event`, which stays correct for `H2H_SIMULATION`'s
  genuinely retrospective past-completed-gameweek fallback) and switching `LEAGUE_SYNC` to it. Safe
  by construction: `_sync_one_rival` already had graceful
  `PicksAvailability.UNAVAILABLE_PRIVATE_OR_NOT_RELEASED` handling for the case picks genuinely
  aren't out yet, so there was never a real reason to gate the attempt on `finished` at all — the
  outer gate was pure, needless delay on data that was often already sitting there.
- **Resolved, real bug (found and fixed 2026-08-23): the scheduled `DATA_INGESTION` job failed
  every single real cloud-scheduler run.** Caught by the manager questioning whether the scheduler
  actually worked — real evidence: the `jobs` table showed the scheduler genuinely firing on
  schedule (trigger timestamps matched the cron interval), but every `DATA_INGESTION` attempt
  failed with `psycopg.errors.QueryCanceled: canceling statement due to statement timeout`, and
  cron-job.org's own execution history showed the `/internal/scheduler-tick` endpoint alternating
  between client-side timeouts and "output too large" (almost certainly a large HTML error page
  from Render/Cloudflare's edge on a slow request, not the endpoint's real tiny JSON body).
  Root cause: `app.ingestion.normalization.record_price_signal_observations` inserted one
  ~600-row batch (one row per current-season player) as a SINGLE multi-row `INSERT`, which
  Supabase's free-tier database timed out on every real run. Fixed by chunking the insert into
  `_PRICE_SIGNAL_INSERT_CHUNK_SIZE = 100`-row batches — a deliberately different, smaller
  constant from `_MAX_BIND_PARAMS`-based chunking used elsewhere in the same file (that limit is
  Postgres' protocol-level bound-parameter cap, ~8000+ rows before it would even trigger for an
  8-column row; this is a real-world execution-time limit, unrelated to parameter count).
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
