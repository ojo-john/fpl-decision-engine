.PHONY: setup migrate test test-integration run ingest train recommend review-gameweek backtest full-pipeline

setup:
	@command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
	uv sync
	@test -f .env || cp .env.example .env

migrate:
	uv run alembic upgrade head

test:
	uv run pytest tests/unit -v

test-integration:
	uv run pytest tests/integration -v

run:
	uv run uvicorn apps.api.main:app --reload --host 0.0.0.0 --port 8000

ingest:
	uv run python scripts/run_job.py --job-type data_ingestion --requested-by local --trigger-type local

train recommend review-gameweek backtest full-pipeline:
	@echo "make $@: not yet implemented — planned for a later milestone (out of scope for Milestone 1)"
