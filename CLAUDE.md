# Project instructions for agents working in this repo

## Keep ARCHITECTURE.md current — this is not optional

`ARCHITECTURE.md` at the repo root is the ground-truth map of this system,
built by reading the actual code rather than trusting existing docs (most of
which — `README.md`, `docs/`, several `app/*/__init__.py` files — were found
stale, still describing early-milestone placeholders for packages that are
now fully built).

**Any agent that changes something ARCHITECTURE.md describes must update it
in the same session, before calling the work done.** This includes but is
not limited to: adding/removing a `JobType` or its handler, adding/removing
an API route, adding a new ingestion pipeline or data source, changing the
optimization gate's logic, adding/removing a database table or a
load-bearing FK relationship, adding a new frontend page or test suite,
changing a CI/scheduling workflow. If a change doesn't obviously map to an
existing section, add one rather than skipping the update.

Do not patch ARCHITECTURE.md from memory or from a commit message — reverify
the specific claim against the real code first, the same way the document
was originally built (see its own "Methodology" section at the bottom).

## NEVER run the backend test suite against the real dev database

`tests/integration/` uses a real Postgres by design (see its own established
"never truncated, must be robust against a shared DB" discipline) — but that
Postgres **must be `fpl_decision_engine_test`, never `fpl_decision_engine`**
(the one `docker-compose.yml`'s `api`/`web` services actually serve, and
that a human may be looking at live in the browser). This is not
theoretical: it happened for real in this session. `tests/integration`'s
fixtures create synthetic seasons/players (`"Player1"`, `"Player2"`, ...)
and — critically — `app/ingestion/service.py` globally unsets
`seasons.is_current` on EVERY row before setting its own season current, so
one test run flipped the real dev database's current season onto a
throwaway synthetic one; every real page started showing fake player names
until it was manually fixed.

**Always** run backend tests via:

```
set -a; source .env.test; set +a
uv run pytest tests/unit -q
uv run pytest tests/integration -q   # this repo's own protocol: run twice
```

Never `source .env` (no suffix) before running pytest or `scripts/run_job.py`
for experimentation — that file's `DATABASE_URL` points at the real
database. `.env.test`/`.env.test.example` at the repo root document the
one-time setup (`CREATE DATABASE fpl_decision_engine_test` + `alembic
upgrade head` against it) if it doesn't exist yet on a given machine.

## Don't trust a docstring's claim about what does/doesn't exist

This codebase has a demonstrated pattern of stale module-level docstrings
(`app/*/__init__.py` files literally reading "Placeholder — implemented in a
later milestone" for packages that are fully built; the `JobType` enum's own
docstring claiming only one handler exists when 16 of 18 do). Before stating
that something is unbuilt, unused, or a stub, check the real code (e.g.
`app/jobs/registry.py`'s actual `@register_handler` calls, actual router
decorators in `apps/api/routers/`), not a comment describing it.
