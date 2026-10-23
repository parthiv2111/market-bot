"""Explicit PostgreSQL connections with UTC sessions and test-target checks."""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url


def database_engine(url: str, *, testing: bool = False) -> Engine:
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("PostgreSQL with the psycopg driver is required")
    if not parsed.database:
        raise ValueError("A database name is required")
    if testing and not parsed.database.endswith("_test"):
        raise ValueError("Tests require a dedicated database ending in _test")
    if not testing and parsed.database.endswith("_test"):
        raise ValueError("Application connections cannot use the test database")
    return create_engine(
        parsed,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": "-c timezone=UTC"},
    )


def database_healthy(engine: Engine) -> bool:
    with engine.connect() as connection:
        return bool(connection.scalar(text("SELECT 1")) == 1)
