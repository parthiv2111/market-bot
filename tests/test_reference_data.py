from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from prediction_bot.reference_data import (
    ExchangeDay,
    InstrumentAlias,
    ReferenceBundle,
    ReferenceStore,
)


@pytest.fixture
def bundle():
    # Invented calendar, including a special Sunday session. Not an NSE schedule.
    return ReferenceBundle(
        source="synthetic-test-only",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 5),
        aliases=(
            InstrumentAlias(
                instrument_id="stable-1",
                exchange_id="ex-1",
                symbol="OLD",
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 1, 4),
            ),
            InstrumentAlias(
                instrument_id="stable-1",
                exchange_id="ex-1",
                symbol="NEW",
                valid_from=date(2026, 1, 4),
                valid_to=date(2027, 1, 1),
            ),
        ),
        days=(
            ExchangeDay(
                session_date=date(2026, 1, 2),
                status="open",
                reason="ordinary",
                opens_at="2026-01-02T09:15:00+05:30",
                closes_at="2026-01-02T15:30:00+05:30",
            ),
            ExchangeDay(session_date=date(2026, 1, 3), status="closed", reason="weekend"),
            ExchangeDay(
                session_date=date(2026, 1, 4),
                status="open",
                reason="special",
                opens_at="2026-01-04T18:00:00+05:30",
                closes_at="2026-01-04T19:00:00+05:30",
            ),
            ExchangeDay(session_date=date(2026, 1, 5), status="closed", reason="holiday"),
        ),
    )


def test_rename_is_effective_dated(bundle):
    old = bundle.resolve("OLD", date(2026, 1, 3))
    new = bundle.resolve("NEW", date(2026, 1, 4))
    assert old.instrument_id == new.instrument_id
    with pytest.raises(LookupError):
        bundle.resolve("OLD", date(2026, 1, 4))
    with pytest.raises(LookupError):
        bundle.resolve("NEW", date(2026, 1, 3))


def test_closure_is_not_missing_observation(bundle):
    assert bundle.observation_status(date(2026, 1, 2), observed=False) == "missing_observation"
    assert bundle.observation_status(date(2026, 1, 3), observed=False) == "market_closed"
    assert bundle.observation_status(date(2026, 1, 5), observed=False) == "market_closed"
    assert bundle.observation_status(date(2026, 1, 4), observed=False) == "missing_observation"
    assert bundle.observation_status(date(2026, 1, 3), observed=True) == "unexpected_observation"
    with pytest.raises(LookupError, match="coverage"):
        bundle.observation_status(date(2026, 1, 6), observed=False)


def test_timezone_and_special_session_boundaries(bundle):
    assert bundle.session_at(datetime(2026, 1, 2, 3, 45, tzinfo=UTC)) is not None
    assert bundle.session_at(datetime(2026, 1, 2, 10, 0, tzinfo=UTC)) is None
    assert bundle.session_at(datetime(2026, 1, 3, 20, 0, tzinfo=UTC)) is None
    assert bundle.session_at(datetime(2026, 1, 4, 12, 30, tzinfo=UTC)).reason == "special"
    with pytest.raises(ValueError):
        bundle.session_at(datetime(2026, 1, 2, 9))
    assert bundle.sessions_after(date(2026, 1, 2), 1) == (date(2026, 1, 4),)
    with pytest.raises(LookupError):
        bundle.sessions_after(date(2026, 1, 2), 2)


def test_missing_or_duplicate_days_and_overlapping_aliases_rejected(bundle):
    values = bundle.model_dump()
    for days in [values["days"][:-1], (*values["days"][:-1], values["days"][0])]:
        with pytest.raises(ValidationError):
            ReferenceBundle.model_validate({**values, "days": days})
    with pytest.raises(ValidationError, match="ambiguous"):
        ReferenceBundle.model_validate(
            {**values, "aliases": (*values["aliases"], values["aliases"][0])}
        )


@pytest.mark.parametrize(
    "open_time,close_time",
    [
        ("2026-01-02T09:15:00", "2026-01-02T15:30:00"),
        ("2026-01-02T15:30:00+05:30", "2026-01-02T09:15:00+05:30"),
        ("2026-01-01T09:15:00+05:30", "2026-01-02T15:30:00+05:30"),
    ],
)
def test_invalid_session_times(open_time, close_time):
    with pytest.raises(ValidationError):
        ExchangeDay(
            session_date=date(2026, 1, 2),
            status="open",
            reason="test",
            opens_at=open_time,
            closes_at=close_time,
        )


def test_versions_survive_restart_and_detect_corruption(bundle, tmp_path):
    source = tmp_path / "input.json"
    source.write_text(bundle.model_dump_json())
    store = ReferenceStore(tmp_path / "registry")
    first = store.import_file(source)
    assert store.import_file(source) == first
    assert ReferenceStore(store.directory).get(first) == bundle
    revised = bundle.model_dump()
    revised["source"] = "synthetic-revision"
    source.write_text(ReferenceBundle.model_validate(revised).model_dump_json())
    second = store.import_file(source)
    assert second != first
    assert store.get(first) == bundle
    (store.directory / f"{first}.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        store.get(first)
