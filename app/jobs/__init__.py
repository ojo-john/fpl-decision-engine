"""Job execution framework: runner base classes, registry, and orchestration service."""

# Importing the handlers subpackage here ensures every handler's
# @register_handler decorator runs (populating app.jobs.registry.JOB_HANDLERS)
# as soon as anything imports app.jobs or one of its submodules — so callers
# like scripts/run_job.py don't need to remember to import handlers directly.
from app.jobs import handlers  # noqa: F401,E402
