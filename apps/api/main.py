"""FastAPI application entrypoint."""

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.auth import require_shared_token
from apps.api.routers import (
    chips,
    h2h,
    health,
    leagues,
    learning,
    managers,
    models,
    optimization,
    players,
    recommendations,
)
from packages.shared.config import get_settings
from packages.shared.logging import configure_logging

configure_logging()


def create_app() -> FastAPI:
    app = FastAPI(title="FPL Decision Engine API")

    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # health stays unauthenticated (see apps.api.auth's own docstring); every
    # other router requires the shared token.
    app.include_router(health.router)
    protected = Depends(require_shared_token)
    app.include_router(players.router, dependencies=[protected])
    app.include_router(optimization.router, dependencies=[protected])
    app.include_router(managers.router_managers, dependencies=[protected])
    app.include_router(managers.router_squads, dependencies=[protected])
    app.include_router(h2h.router, dependencies=[protected])
    app.include_router(chips.router, dependencies=[protected])
    app.include_router(recommendations.router, dependencies=[protected])
    app.include_router(learning.router_jobs, dependencies=[protected])
    app.include_router(learning.router_learning, dependencies=[protected])
    app.include_router(leagues.router, dependencies=[protected])
    app.include_router(models.router, dependencies=[protected])
    return app


app = create_app()
