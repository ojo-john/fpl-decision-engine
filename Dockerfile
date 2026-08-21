FROM python:3.12-slim

# Install uv (static binary, no pip/network dependency at runtime).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

EXPOSE 8000

# Runs migrations on every container start, not just first boot -- deliberate: this is the one
# mechanism guaranteed to run regardless of hosting plan (Render's preDeployCommand and Shell tab
# are both paid-tier-only; local docker-compose.yml overrides `command:` for the api/scheduler
# services, so this default CMD never runs there and this has no effect on local dev). `alembic
# upgrade head` is idempotent -- a no-op once the schema is current -- so running it on every
# start (including free-tier cold-start wake-ups) is safe.
CMD ["sh", "-c", "uv run alembic upgrade head && exec uv run uvicorn apps.api.main:app --host 0.0.0.0 --port 8000"]
