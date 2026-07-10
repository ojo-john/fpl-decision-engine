"""Shared FastAPI dependencies."""

from collections.abc import Generator

from sqlalchemy.orm import Session

from packages.database.session import SessionLocal


def get_db() -> Generator[Session, None, None]:
    """Yield a database session, closing it once the request is done."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
