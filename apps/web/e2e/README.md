# End-to-end tests (Playwright)

Real `@playwright/test` specs driving the actual app in a real browser against a real,
running backend — this replaced a session's worth of ad-hoc one-off verification scripts with
a committed, repeatable suite.

## Running locally

1. Bring up Postgres + the API (`docker compose up -d postgres api` from the repo root, or the
   full stack). The API must be reachable at `http://localhost:8000` with
   `APP_SHARED_TOKEN=dev-token-change-me` (the compose stack's own dev default).
2. Make sure there's real data to browse: `make ingest` from the repo root (hits the real,
   live FPL API — see "Why this isn't in CI" below).
3. `npm run test:e2e` from `apps/web/`. `playwright.config.ts`'s `webServer` option starts
   `next dev` on port 3002 automatically — no separate `npm run dev` needed. Port 3000 (the
   `web` service in docker-compose) and 3001 (used by ad-hoc manual verification scripts) are
   both deliberately left free so this suite never collides with either.

No `playwright install` step needed: `playwright.config.ts` points `launchOptions.executablePath`
at this machine's own installed Google Chrome rather than downloading Playwright's browser
binaries.

## Why this isn't wired into `ci.yml`

`ci.yml`'s `web` job today only runs `lint`/`typecheck`/`build` — this suite is deliberately
NOT added to it. Seeding real, UI-testable data (players to browse, a real recommendation to
view) needs either:

- the live FPL API (`make ingest`) — non-deterministic and rate-limited, exactly the kind of
  external dependency that shouldn't gate a CI run, or
- the monkeypatched FPL client `tests/integration/conftest.py` uses — that only exists inside
  the pytest process itself (`monkeypatch` on an in-process HTTP client), unreachable by an
  externally-driven Playwright run against a separately-running `uvicorn`/`next` server.

Running this suite in CI would need a real seed strategy decision (a recorded fixture replay
server standing in for the FPL API, most likely) that's out of scope here. This is a
deliberate scope boundary, not an oversight — flag it if CI coverage of the UI becomes a real
priority.

## What's covered

- `auth.spec.ts` — wrong token rejected, correct token logs in, logout re-guards the app.
- `players.spec.ts` — list renders real (2026-27 season) players, position filter, search,
  detail page with live projection + season stats.
- `team.spec.ts` — full manual-squad lifecycle: build a position-legal 15-player squad
  (2 GKP/5 DEF/5 MID/3 FWD — an arbitrary 15 players can be solver-infeasible, see
  `app/jobs/handlers/h2h_simulation.py`'s own fallback tests for the same real gotcha), create
  it, duplicate-and-edit (confirms the editor comes back pre-filled), delete it, and confirm
  it's really gone via a direct API call.
- `pages-render.spec.ts` — Dashboard/Rivals/History/Learning render one of their known-valid
  states (empty or real content — this suite runs against a shared, non-truncated database
  other work has already written into, so it never assumes which) with no real console errors.

Not covered here: optimization/transfer/chip/H2H POST triggers, since the currently-ingested
season is pre-season (`optimization_permitted=False`, every one of those would 409 regardless
of UI correctness) — that's a data-state fact of the shared dev database at the time this
suite was written, not a suite limitation; re-run against an in-season database to exercise
them.
