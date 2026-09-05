# Deployment (free-tier cloud hosting)

Everything else in this repo (docs/operations.md's own local scheduler section included) assumes
you're running this app locally via `docker compose up` -- real, and the right default for most of
this project's life, but it only works while your own machine is on. This document covers the
real alternative: hosting the app on real, free-tier cloud services so you can reach your FPL
dashboard from your phone even when your laptop is off.

## Why this specific combination

Real, current (2026) research into free tiers across Railway/Render/Fly.io/Supabase/Vercel found
that no single provider offers a fully free, always-on, zero-compromise stack for this app's real
shape (Postgres + a Python API + a Next.js frontend + a periodic background task). The real,
workable combination below accepts two honest tradeoffs instead:

- **Render's free web service spins down after 15 minutes of inactivity.** The first request
  after a gap has a real ~30-60 second cold-start delay while it wakes back up. Fine for checking
  your team a few times a day; noticeable if you want instant responses constantly.
- **Supabase's free Postgres project auto-pauses after 7 days of zero activity.** Your data is
  retained and it resumes automatically on the next request -- but since the scheduler tick
  (below) pings it at least every 15 minutes whenever it runs, this should never actually trigger
  in practice.
- There is no free "persistent background worker" tier that keeps `scripts/scheduler.py`'s own
  local loop running in the cloud. Instead, `.github/workflows/scheduler-tick.yml` (a scheduled
  GitHub Actions workflow, `7,22,37,52 * * * *`) calls a real HTTP endpoint
  (`POST /internal/scheduler-tick`) on that same schedule, which runs the exact same real scheduling
  logic (`app.scheduling.tick.run_tick`) as the local loop -- see that module's own docstring for
  why this is safe and correct, not a separate, duplicated implementation.
  **Revised 2026-08-26**: originally used a free external cron service (cron-job.org) instead --
  replaced after a real incident where it silently stopped calling the endpoint for 5+ days with no
  visible error anywhere (see `scheduler-tick.yml`'s own docstring for the full incident).
  GitHub Actions gives this a real, visible run history in this repo's own Actions tab, the same
  place every other real job in this project is already watched, rather than a third-party
  dashboard that wasn't being checked.

| Piece | Provider | What it does |
|---|---|---|
| Postgres | [Supabase](https://supabase.com) | Your real database, replacing the local `postgres` Docker service. |
| Backend | [Render](https://render.com) | Runs the same `Dockerfile` this repo's `docker-compose.yml` `api` service already uses. |
| Frontend | [Vercel](https://vercel.com) | Runs `apps/web`, Next.js's own natural home. |
| Scheduler | GitHub Actions (`scheduler-tick.yml`) | Calls `POST /internal/scheduler-tick` on Render every 15 minutes, replacing the local `scheduler` Docker service. |

## Setup, in order

You need to do these in this order -- each later step needs a real value (a connection string, a
URL) produced by the step before it.

### 1. Supabase (Postgres)

1. Create a free account and a new project at [supabase.com](https://supabase.com).
2. In your new project's dashboard, go to **Project Settings -> Database -> Connection string**
   and copy the real connection string (choose the "URI" / connection-pooling variant Supabase
   recommends for serverless/external connections).
3. Rewrite it into this app's expected SQLAlchemy form -- it needs a `postgresql+psycopg://`
   prefix (SQLAlchemy's driver-qualified scheme), not the plain `postgresql://` Supabase gives you
   by default. Example shape:
   ```
   postgresql+psycopg://<user>:<password>@<host>:<port>/<database>
   ```
4. Keep this real connection string somewhere safe -- you'll paste it into Render as `DATABASE_URL`
   in the next step.
5. You do NOT need to run `alembic upgrade head` against this manually right now -- Render's own
   build step (configured below) runs it automatically on every deploy.

### 2. Render (backend API)

1. Create a free account at [render.com](https://render.com), connect your GitHub account, and
   give it access to this repository.
2. In Render's dashboard, create a new **Blueprint** and point it at this repo -- it will detect
   the committed `render.yaml` at the repo root automatically.
3. Render will prompt you to fill in every `envVars` entry marked `sync: false` in that file. Set
   real values for each (see `render.yaml`'s own comments for exactly what each one is):
   - `DATABASE_URL` -- your real Supabase connection string from step 1.
   - `APPLICATION_SECRET_KEY` -- any random string you generate yourself (not the local dev
     placeholder).
   - `APP_SHARED_TOKEN` -- any random string you generate yourself. This is the single bearer
     token the frontend and the GitHub Actions scheduler (step 4 below) will both need to use
     later -- write it down.
   - `CORS_ALLOWED_ORIGINS` -- leave a placeholder for now (e.g. `https://placeholder.vercel.app`)
     -- you'll come back and set the real value after step 3 (Vercel) gives you a real URL.
   - `FPL_MANAGER_ID` / `FPL_H2H_LEAGUE_ID` -- your own real, numeric FPL manager and H2H league
     ids.
   - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` -- a real Web Push keypair (Milestone 9 Layer 1,
     proactive alerts). Generate one with `uv run vapid --gen` (from this repo, `py-vapid` is
     already a `pywebpush` dependency) -- it writes `private_key.pem`/`public_key.pem` in your
     current directory.
     - `VAPID_PRIVATE_KEY` is the **full real contents** of `private_key.pem`, including the
       `-----BEGIN PRIVATE KEY-----`/`-----END PRIVATE KEY-----` lines (`app.notifications
       .push_sender` parses this exact PEM text directly -- not a file path, not a bare base64
       string).
     - `VAPID_PUBLIC_KEY` is the applicationServerKey string, printed by running
       `uv run vapid --applicationServerKey` afterward (a single base64url line) -- this is what
       the frontend's own `pushManager.subscribe()` call needs, fetched at runtime via
       `GET /push/vapid-public-key`, never build-time-baked.
     - Delete both `.pem` files once you've copied the values into Render's dashboard -- they're
       real secrets, don't commit them (already covered by this repo's `*.pem` gitignore entry).
   - `VAPID_SUBJECT` -- a real contact URI (`mailto:you@example.com` is fine), required by the
     Web Push spec so a push service can reach you about a misbehaving application server.
   - `GITHUB_ACTIONS_TRIGGER_TOKEN` -- Milestone 9 Layer 5, the FPL dashboard's own "Retrain
     model" button (see "Retraining the model against real data" below for the full picture). A
     **fine-grained** GitHub personal access token, scoped as narrowly as possible:
     [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new) ->
     **Repository access**: "Only select repositories" -> this repo only -> **Permissions** ->
     **Actions**: Read and write (the only permission this needs). Copy the token (`github_pat_...`)
     immediately -- GitHub only shows it once.
4. Deploy. The container's own startup command runs `uv run alembic upgrade head` before
   starting the API (see the `Dockerfile`'s own comment) -- no manual dashboard step needed, and
   no dependency on `preDeployCommand` or the Shell tab, both of which turned out to be
   paid-tier-only on Render's free plan (confirmed 2026-08-21: `preDeployCommand` silently didn't
   run, and free instances get "Shell is not supported for free instance types" instead of a
   working Shell tab). Since this runs on every container start, not just the first one, it's
   safe and idempotent -- including the free tier's cold-start wake-ups after 15 minutes idle.
5. Once deployed, Render gives you a real URL (e.g. `https://fpl-decision-engine-api.onrender.com`).
   Confirm it's real and working: `curl https://<your-real-render-url>/health` should return
   `{"status":"ok"}` (allow up to a minute for the free tier's cold start on the very first hit).

### 3. Vercel (frontend)

1. Create a free account at [vercel.com](https://vercel.com), connect GitHub, import this repo as
   a new project.
2. **Root Directory**: in the project's own setup screen (or later under Settings -> Build and
   Deployment), set this to `apps/web` -- this repo is a monorepo, and Vercel needs to know the
   actual Next.js app lives in a subdirectory, not the repo root. Changing this mid-import
   correctly re-detects the Framework Preset as Next.js in the dashboard, but a real deploy
   (2026-08-21) still failed with `Error: No Output Directory named "public" found` despite the
   dashboard showing Next.js correctly -- a known real issue with Next.js 16 + Turbopack in
   monorepos where auto-detection after a Root Directory change doesn't reliably carry through to
   the actual build worker. `apps/web/vercel.json` (`{"framework": "nextjs"}`) fixes this by
   declaring the framework explicitly in a committed file, which takes precedence over the
   dashboard-inferred value -- no dashboard action needed for this part, it's already committed.
3. Set two real environment variables in Vercel's dashboard (Settings -> Environment Variables):
   - `API_BASE_URL` -- your real Render URL from step 2 (e.g.
     `https://fpl-decision-engine-api.onrender.com`).
   - `APP_SHARED_TOKEN` -- the exact same random string you generated for Render in step 2.3.
4. Deploy. Vercel gives you a real URL (e.g. `https://your-app.vercel.app`).
5. **Go back to Render** and update `CORS_ALLOWED_ORIGINS` to this real Vercel URL (replacing the
   placeholder from step 2.3), then redeploy the Render service so the change takes effect.

### 4. GitHub Actions (scheduler)

**Revised 2026-08-26** -- previously cron-job.org; replaced after it silently stopped calling
`/internal/scheduler-tick` for 5+ real days with no visible error anywhere (see
`.github/workflows/scheduler-tick.yml`'s own docstring for the full incident). No third-party
account needed now -- the workflow file is already committed, it just needs one secret:

1. In your GitHub repo, go to **Settings -> Secrets and variables -> Actions -> New repository
   secret**.
2. Name: `APP_SHARED_TOKEN`. Value: the **exact same** random string you generated for Render's own
   `APP_SHARED_TOKEN` in step 2.3 above.
3. That's it -- `scheduler-tick.yml` is already scheduled (`7,22,37,52 * * * *`, matching
   `scripts/scheduler.py`'s own local tick interval) and needs no further setup. Confirm it's
   working: **Actions tab -> Scheduler tick -> Run workflow** (manual `workflow_dispatch` trigger,
   doesn't require waiting for the schedule) -- a green run confirms the whole chain end to end.

## Verification

1. Visit your real Vercel URL in a browser, log in with your real `APP_SHARED_TOKEN`, and confirm
   the dashboard loads (expect real data once the scheduler's first `DATA_INGESTION` tick has run
   -- see step 2 below).
2. Wait ~15-20 minutes, then check the **Actions tab -> Scheduler tick** run history for a real
   green run. Check Render's own logs (Dashboard -> your service -> Logs) for
   `scheduler.job_completed`/`scheduler.triggering_data_ingestion` entries confirming a real tick
   actually ran.
3. `curl -X POST https://<your-real-render-url>/internal/scheduler-tick -H "Authorization: Bearer <your-real-token>"`
   should return `{"status":"tick_completed"}` with a `200` -- safe to run this manually any time
   to force an immediate check rather than waiting for the next scheduled run.

## Retraining the model against real data

`POST /models/train` (via Render) runs `MODEL_TRAINING` in-process on the free-tier web service --
real incident (2026-08-25): this OOM-crashed the service (512MB RAM) mid-training, twice, even
after cutting tree ensemble sizes 75% (see `app.modeling.candidates`' own
`RANDOM_FOREST_N_ESTIMATORS`/`GRADIENT_BOOSTING_MAX_ITER` history) -- there just isn't enough free
RAM there for this specific workload. Use the **Train (production database)** GitHub Actions
workflow instead (`.github/workflows/train-production.yml`) -- GitHub's own `ubuntu-latest` runners
have ~7GB RAM, comfortably enough, no model-quality tradeoff required.

One-time setup:

1. In your GitHub repo, go to **Settings -> Secrets and variables -> Actions -> New repository
   secret**.
2. Name: `PROD_DATABASE_URL`. Value: the exact same real Supabase connection string you set as
   Render's own `DATABASE_URL` (step 1 above).

To run it, either:
- **From the FPL dashboard itself**: the "Retrain model (production)" button on the Learning page
  (`POST /models/train-production`) -- dispatches this same workflow remotely via GitHub's REST
  API, using the `GITHUB_ACTIONS_TRIGGER_TOKEN` secret set above. No GitHub UI needed; the button
  links out to the run's own Actions page to watch progress.
- **From GitHub directly**: **Actions tab -> Train (production database) -> Run workflow**.

`season_label` defaults
to `"all"` (every season with ingested `player_gameweek_stats`) -- leave it unless you specifically
want to retrain against just one season. Check the run's own Job Summary (or the uploaded
`train-production-job-summary` artifact) for the real `promotion_outcome` (`"champion"` -- promoted
immediately -- vs `"shadow_challenger"` -- a real, usable champion already existed, this run queued
a challenger instead) and per-candidate `aggregate_mae`/`aggregate_spearman`.

This workflow's `DATABASE_URL` points at the same real, persistent database Render's own API
serves from (unlike `train.yml`/`ingest.yml`/`historical-ingest.yml`/`feature-generation.yml`,
which all spin up their own throwaway, empty Postgres service container inside the runner -- see
`ARCHITECTURE.md`'s own "Cloud deployment" section for why) -- a real `MODEL_TRAINING` run here
genuinely updates what your live dashboard serves.

## What this does NOT replace

The local `docker compose up` + `scripts/scheduler.py` setup (see `docs/operations.md`) is still
the right choice for active development -- faster iteration, no free-tier cold-start delay, full
control. This deployment is specifically for phone/away-from-laptop access to the live dashboard,
not a development environment.
