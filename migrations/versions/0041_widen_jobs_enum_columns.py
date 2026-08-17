"""widen jobs.job_type/status/trigger_type columns

Real bug found 2026-08-17 while running POSITION_SEGMENTATION_VALIDATION against the real dev
database: ``jobs.job_type`` is a ``native_enum=False`` ``VARCHAR`` whose length was fixed by
Alembic at whatever ``JobType``'s longest member was the FIRST TIME migration 0001 was applied
to a given database -- not a portable, explicit contract (see 0006's own precedent for this
exact class of bug, which correctly widened ``ingestion_runs.run_type`` but explicitly reasoned
``jobs.job_type`` didn't need it *at the time*, since ``"historical_data_ingestion"`` (25 chars)
exactly fit). Three job types added this session --
``start_probability_model_training``/``cold_start_projection_validation``/
``position_segmentation_validation`` (32 chars each) -- now exceed that frozen 25-char limit.
Confirmed via a live ``information_schema``/psql query on the real dev database (not
recomputed from the current Python enum alone, since a fresh test database created via
``alembic upgrade head`` today auto-sizes the column against the CURRENT enum and never
exhibits this bug at all -- only a database whose ``0001`` ran before today's job types existed
has this frozen-narrow column, which is exactly why this was invisible to every integration
test run this session and only surfaced against the real dev database).

Every real invocation of these 3 job types against the real dev database was failing with
``psycopg.errors.StringDataRightTruncation`` before this fix -- a genuinely broken feature, not
a cosmetic issue.

Also widens ``jobs.status``/``jobs.trigger_type`` proactively: same table, same structural risk
class (both are also un-lengthed ``native_enum=False`` columns whose real width depends on
migration-run-time enum state), currently fitting only by coincidence (`"cancelled"` = 9,
`"github_schedule"` = 15 -- both exactly at their current real column width). Sized generously
(50) rather than exactly, so the next enum member added doesn't silently reproduce this bug
again. See ``tests/unit/test_job_enums.py``'s new column-width regression test, which asserts
every current member of all three enums fits the real column width chosen here -- so a future
enum addition that would break this fails CI immediately instead of silently succeeding against
a fresh test database while failing against any already-migrated one.

This is a real, systemic risk class shared by ~25+ other ``native_enum=False`` columns across
this schema (see ``ARCHITECTURE.md``'s "Known real gaps" for the full list) -- this migration
fixes the two tables confirmed broken tonight (``jobs``), not a full schema-wide audit.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "jobs",
        "job_type",
        type_=sa.String(length=50),
        existing_type=sa.String(length=25),
        existing_nullable=False,
    )
    op.alter_column(
        "jobs",
        "status",
        type_=sa.String(length=50),
        existing_type=sa.String(length=9),
        existing_nullable=False,
    )
    op.alter_column(
        "jobs",
        "trigger_type",
        type_=sa.String(length=50),
        existing_type=sa.String(length=15),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "jobs",
        "trigger_type",
        type_=sa.String(length=15),
        existing_type=sa.String(length=50),
        existing_nullable=False,
    )
    op.alter_column(
        "jobs",
        "status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=50),
        existing_nullable=False,
    )
    op.alter_column(
        "jobs",
        "job_type",
        type_=sa.String(length=25),
        existing_type=sa.String(length=50),
        existing_nullable=False,
    )
