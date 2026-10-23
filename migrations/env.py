"""Alembic entry point; URL comes from the environment, never tracked secrets."""

import os

from alembic import context

from prediction_bot.persistence.database import database_engine
from prediction_bot.persistence.schema import metadata


def run_migrations():
    provided = context.config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = os.environ.get("PREDICTION_BOT_DATABASE_URL")
    if not url:
        raise RuntimeError("PREDICTION_BOT_DATABASE_URL is required")
    engine = database_engine(url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
