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
  local loop running in the cloud. Instead, a free external cron service
  ([cron-job.org](https://cron-job.org)) calls a real HTTP endpoint
  (`POST /internal/scheduler-tick`) on a schedule, which runs the exact same real scheduling logic
  (`app.scheduling.tick.run_tick`) as the local loop -- see that module's own docstring for why
  this is safe and correct, not a separate, duplicated implementation.

| Piece | Provider | What it does |
|---|---|---|
| Postgres | [Supabase](https://supabase.com) | Your real database, replacing the local `postgres` Docker service. |
| Backend | [Render](https://render.com) | Runs the same `Dockerfile` this repo's `docker-compose.yml` `api` service already uses. |
| Frontend | [Vercel](https://vercel.com) | Runs `apps/web`, Next.js's own natural home. |
| Scheduler | [cron-job.org](https://cron-job.org) | Calls `POST /internal/scheduler-tick` on Render every 15 minutes, replacing the local `scheduler` Docker service. |

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
     token the frontend and cron-job.org will both need to use later -- write it down.
   - `CORS_ALLOWED_ORIGINS` -- leave a placeholder for now (e.g. `https://placeholder.vercel.app`)
     -- you'll come back and set the real value after step 3 (Vercel) gives you a real URL.
   - `FPL_MANAGER_ID` / `FPL_H2H_LEAGUE_ID` -- your own real, numeric FPL manager and H2H league
     ids.
4. Deploy. Render will build the real Dockerfile, run `alembic upgrade head` is NOT automatic in
   the base Dockerfile's own `CMD` -- add a **Pre-Deploy Command** in Render's dashboard
   (Settings -> Build & Deploy) of `uv run alembic upgrade head` so every deploy migrates your real
   Supabase database first.
5. Once deployed, Render gives you a real URL (e.g. `https://fpl-decision-engine-api.onrender.com`).
   Confirm it's real and working: `curl https://<your-real-render-url>/health` should return
   `{"status":"ok"}` (allow up to a minute for the free tier's cold start on the very first hit).

### 3. Vercel (frontend)

1. Create a free account at [vercel.com](https://vercel.com), connect GitHub, import this repo as
   a new project.
2. **Root Directory**: in the project's own setup screen (or later under Settings -> General),
   set this to `apps/web` -- this repo is a monorepo, and Vercel needs to know the actual Next.js
   app lives in a subdirectory, not the repo root. (Verified against Vercel's own real, current
   docs -- this is a dashboard setting, not something encoded in a committed config file.)
3. Set two real environment variables in Vercel's dashboard (Settings -> Environment Variables):
   - `API_BASE_URL` -- your real Render URL from step 2 (e.g.
     `https://fpl-decision-engine-api.onrender.com`).
   - `APP_SHARED_TOKEN` -- the exact same random string you generated for Render in step 2.3.
4. Deploy. Vercel gives you a real URL (e.g. `https://your-app.vercel.app`).
5. **Go back to Render** and update `CORS_ALLOWED_ORIGINS` to this real Vercel URL (replacing the
   placeholder from step 2.3), then redeploy the Render service so the change takes effect.

### 4. cron-job.org (scheduler)

1. Create a free account at [cron-job.org](https://cron-job.org).
2. Create a new cron job:
   - **URL**: `https://<your-real-render-url>/internal/scheduler-tick`
   - **Method**: `POST`
   - **Headers**: add `Authorization: Bearer <your-real-APP_SHARED_TOKEN>` (the exact same value
     from step 2.3 / 3.3).
   - **Schedule**: every 15 minutes -- matches `scripts/scheduler.py`'s own local tick interval
     (see `app.scheduling.tick`'s own module docstring for why this cadence). cron-job.org's free
     tier supports down to 1-minute granularity, but there's no benefit to calling more often than
     this -- the endpoint's own real interval logic (`DATA_INGESTION` every 4h, `LEAGUE_SYNC` every
     6h) only actually triggers a job when something is genuinely due, regardless of how often
     it's pinged.
3. Save and enable the job.

## Verification

1. Visit your real Vercel URL in a browser, log in with your real `APP_SHARED_TOKEN`, and confirm
   the dashboard loads (expect real data once the scheduler's first `DATA_INGESTION` tick has run
   -- see step 2 below).
2. Wait ~15-20 minutes, then check cron-job.org's own execution history for the job you created --
   it should show a real `200` response. Check Render's own logs (Dashboard -> your service ->
   Logs) for `scheduler.job_completed`/`scheduler.triggering_data_ingestion` entries confirming a
   real tick actually ran.
3. `curl -X POST https://<your-real-render-url>/internal/scheduler-tick -H "Authorization: Bearer <your-real-token>"`
   should return `{"status":"tick_completed"}` with a `200` -- safe to run this manually any time
   to force an immediate check rather than waiting for cron-job.org's own schedule.

## What this does NOT replace

The local `docker compose up` + `scripts/scheduler.py` setup (see `docs/operations.md`) is still
the right choice for active development -- faster iteration, no free-tier cold-start delay, full
control. This deployment is specifically for phone/away-from-laptop access to the live dashboard,
not a development environment.
