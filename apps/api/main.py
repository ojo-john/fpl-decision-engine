"""FastAPI application entrypoint."""

from fastapi import FastAPI

from apps.api.routers import health, optimization, players
from packages.shared.logging import configure_logging

configure_logging()


def create_app() -> FastAPI:
    app = FastAPI(title="FPL Decision Engine API")
    app.include_router(health.router)
    app.include_router(players.router)
    app.include_router(optimization.router)
    return app


app = create_app()
