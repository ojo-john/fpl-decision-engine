#!/usr/bin/env python
"""CLI entrypoint to execute a single job synchronously.

Usage:
    uv run python scripts/run_job.py --job-type system_health \\
        --requested-by me --trigger-type local

Prints a JSON object describing the job outcome to stdout. Never prints
DATABASE_URL or any other environment variable — only fields explicitly
selected from the resulting Job row.
"""

import argparse
import json
import sys

from app.jobs.service import execute_job
from packages.database.session import SessionLocal
from packages.domain.jobs import JobType, TriggerType


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute a single job synchronously.")
    parser.add_argument(
        "--job-type",
        required=True,
        choices=[jt.value for jt in JobType],
        help="Job type to execute.",
    )
    parser.add_argument("--requested-by", required=True, help="Who/what requested this job.")
    parser.add_argument(
        "--trigger-type",
        required=True,
        choices=[tt.value for tt in TriggerType],
        help="How this job run was triggered.",
    )
    parser.add_argument("--idempotency-key", default=None, help="Optional idempotency key.")
    parser.add_argument("--git-commit-sha", default=None, help="Optional git commit SHA to record.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    session = SessionLocal()
    try:
        job = execute_job(
            session,
            job_type=JobType(args.job_type),
            requested_by=args.requested_by,
            trigger_type=TriggerType(args.trigger_type),
            idempotency_key=args.idempotency_key,
            git_commit_sha=args.git_commit_sha,
        )
    finally:
        session.close()

    payload = {
        "job_id": str(job.id),
        "job_type": job.job_type.value if hasattr(job.job_type, "value") else job.job_type,
        "status": job.status.value if hasattr(job.status, "value") else job.status,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "output": job.output_json,
    }
    print(json.dumps(payload))

    status_value = job.status.value if hasattr(job.status, "value") else job.status
    return 0 if status_value == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
