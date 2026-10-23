from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from prediction_bot.persistence.database import database_engine
from prediction_bot.persistence.schema import UTCDateTime


@pytest.mark.parametrize(
    "url,testing",
    [
        ("sqlite://", False),
        ("postgresql+psycopg://localhost/production", True),
        ("postgresql+psycopg://localhost/prediction_bot_test", False),
        ("postgresql+psycopg://localhost", False),
    ],
)
def test_database_target_separation(url, testing):
    with pytest.raises(ValueError):
        database_engine(url, testing=testing)


def test_utc_timestamp_contract():
    timestamp = UTCDateTime()
    dialect = postgresql.dialect()
    with pytest.raises(ValueError, match="timezone"):
        timestamp.process_bind_param(datetime(2026, 1, 1), dialect)
    local = datetime(2026, 1, 1, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert timestamp.process_bind_param(local, dialect) == datetime(2026, 1, 1, 3, 45, tzinfo=UTC)
