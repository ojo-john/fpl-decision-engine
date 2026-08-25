"""model_versions.artifact_bytes / start_probability_models.artifact_bytes

Real production bug this fixes: both tables' `artifact_uri` pointed at a LOCAL filesystem path
(`app/jobs/handlers/model_training.py`'s own `MODELS_DIR = Path("models")`) -- durable on a
long-lived local/docker-compose container, but Render rebuilds and redeploys the entire container
filesystem from scratch on every `git push` to `main`. A model trained in one deploy's container is
gone the moment the next deploy replaces it, even though the `model_versions`/`start_probability_
models` row (in the real, persistent Supabase database) still says a champion is selected --
confirmed live (2026-08-24): `POST /jobs/backtest` against the real deployment raised a raw
`FileNotFoundError` for the champion's own `artifact_uri`.

`champion_loader.py`/`backtest.py`/`start_probability_loader.py` already degrade gracefully when an
artifact can't be loaded (never blocks optimization), so nothing crashed -- but every live
projection had been silently falling back to the plain non-ML formula since whichever deploy first
wiped the trained model's file.

This is additive only: `artifact_uri`/local-disk writing stay exactly as they were (still a useful
local-dev convenience, matches `.gitignore`'s own `models/*` precedent) -- `artifact_bytes` becomes
the new, SOLE authoritative source every loader reads from, stored directly in the same persistent
database the rest of this system already trusts, so it survives every future redeploy the same way
every other real piece of application state already does.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("model_versions", sa.Column("artifact_bytes", sa.LargeBinary(), nullable=True))
    op.add_column("start_probability_models", sa.Column("artifact_bytes", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("start_probability_models", "artifact_bytes")
    op.drop_column("model_versions", "artifact_bytes")
