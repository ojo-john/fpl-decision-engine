"""FastAPI application entrypoint."""

from fastapi import FastAPI

from apps.api.routers import health
from packages.shared.logging import configure_logging

configure_logging()


def create_app() -> FastAPI:
    app = FastAPI(title="FPL Decision Engine API")
    app.include_router(health.router)
    return app


app = create_app()
