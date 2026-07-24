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

recommend:
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.RECOMMENDATION_GENERATION, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"

# EVENT is the target gameweek's raw FPL event number (required -- GAMEWEEK_REVIEW has no
# sensible default, see app/jobs/handlers/gameweek_review.py's own MISSING_TARGET_FPL_EVENT_ID).
review-gameweek:
	@test -n "$(EVENT)" || (echo "usage: make review-gameweek EVENT=<fpl_event_id>" && exit 1)
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.GAMEWEEK_REVIEW, parameters={'target_fpl_event_id': int('$(EVENT)')}, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"

# MODEL_VERSION_ID is required -- BACKTEST has no sensible default (which model would "the"
# backtest even mean?), see app/jobs/handlers/backtest.py's own docstring.
backtest:
	@test -n "$(MODEL_VERSION_ID)" || (echo "usage: make backtest MODEL_VERSION_ID=<uuid>" && exit 1)
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.BACKTEST, parameters={'model_version_id': '$(MODEL_VERSION_ID)'}, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"

full-pipeline:
	uv run python -c "from app.jobs.service import execute_job; from packages.database.session import SessionLocal; from packages.domain.jobs import JobType, TriggerType; session = SessionLocal(); job = execute_job(session, job_type=JobType.FULL_PIPELINE, requested_by='local', trigger_type=TriggerType.LOCAL); print(job.status, job.output_json); session.close()"
