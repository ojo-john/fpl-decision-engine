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
.github/workflows/        ci.yml, health.yml, ingest.yml, scheduled-dispatcher.yml, historical-ingest.yml, feature-generation.yml, train.yml, league-watch.yml (standalone -- see its own section below)
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
fixture list.

`career_avg_early_season_points_prior_seasons` is also a column on the
PERSISTED `player_features` table (migration 0030) for
`FEATURE_GENERATION`/`MODEL_TRAINING`/audit purposes, alongside the
existing `career_avg_points_prior_seasons` — but `_cold_start_projection`
itself never reads that persisted table; it always computes fresh via
`compute_features_for_player_gameweek`, so the cold-start blend
improvement is live immediately, without needing `FEATURE_GENERATION` to
be re-run. A re-run is only needed to backfill the new column on
already-computed historical `player_features` rows.

Both `SQUAD_OPTIMIZATION` and `/optimize/preview` rank candidates by
points summed across the next `SQUAD_BUILD_HORIZON_LENGTH` (6) unfinished
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
`ep_next`, `career_avg_points_prior_seasons`,
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
`champion_model_family` correctly stays `None` in that case. The
explanation text correctly names
`career_avg_early_season_points_prior_seasons` (not the stale full-season
number) whenever `_cold_start_projection` actually used it — fixed in the
same session `career_avg_early_season_points_prior_seasons` was added.
See `apps.api.routers.players._explain_base_projection`/`_explain_projection`
for the exact source-string-to-sentence mapping (the latter strips
`app.optimization.projection.AVAILABILITY_RISK_NOTED_SUFFIX` before
delegating to the former, then appends a risk-note sentence) and
`app.optimization.projection.is_cold_start_season` (a thin public wrapper
around the module's own `_is_cold_start_season`, added for this endpoint).

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

45 ORM models under `packages/database/models/`. The load-bearing shape:

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
