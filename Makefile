.PHONY: setup migrate test test-integration run ingest features train recommend review-gameweek backtest full-pipeline

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

features:
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.FEATURE_GENERATION, parameters={'season_label': 'all'}, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"

train:
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.MODEL_TRAINING, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"

recommend review-gameweek backtest full-pipeline:
	@echo "make $@: not yet implemented — planned for a later milestone"
