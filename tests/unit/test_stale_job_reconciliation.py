"""Unit test for app.jobs.handlers.stale_job_reconciliation's pure threshold lookup.

No DB -- the actual RUNNING-row scan/finalize logic needs a real database and is covered by
tests/integration/test_stale_job_reconciliation_job.py instead (mirrors this repo's own
test_deadline_reminder_job.py unit/integration split).
"""

from datetime import timedelta

from app.jobs.handlers.stale_job_reconciliation import (
    _DEFAULT_STALE_THRESHOLD,
    _STALE_THRESHOLDS,
    stale_threshold_for,
)
from packages.domain.jobs import JobType


def test_known_job_type_returns_its_own_threshold():
    assert stale_threshold_for(JobType.TRANSFER_OPTIMIZATION) == timedelta(minutes=90)


def test_unlisted_job_type_falls_back_to_default():
    assert stale_threshold_for(JobType.FULL_PIPELINE) == _DEFAULT_STALE_THRESHOLD


def test_every_listed_threshold_is_below_the_default_ceiling():
    """Real-world sanity check: every per-job-type threshold is a deliberate REDUCTION from the
    conservative default, grounded in an observed real duration -- not an arbitrary number that
    happens to exceed it (which would make the per-type entry pointless)."""
    for job_type, threshold in _STALE_THRESHOLDS.items():
        assert threshold < _DEFAULT_STALE_THRESHOLD, job_type
