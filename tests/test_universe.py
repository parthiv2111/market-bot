from datetime import UTC, date, datetime

import pytest

from prediction_bot.reference_data import ExchangeDay, InstrumentAlias, ReferenceBundle
from prediction_bot.universe import (
    AvailableObservation,
    Constituent,
    ConstituentSnapshot,
    HistoryLimitation,
    Universe,
    UniverseStore,
    coverage_report,
)


def instant(day, hour=12):
    return datetime(2026, 1, day, hour, tzinfo=UTC)


@pytest.fixture
def universe():
    return Universe(
        name="Synthetic index",
        snapshots=(
            ConstituentSnapshot(
                valid_from=date(2026, 1, 2),
                valid_to=date(2026, 1, 4),
                known_at=instant(1),
                source="synthetic historical list",
                members=(Constituent(instrument_id="old", symbol="OLD"),),
            ),
            ConstituentSnapshot(
                valid_from=date(2026, 1, 4),
                valid_to=date(2026, 1, 7),
                known_at=instant(3),
                source="synthetic replacement list",
                members=(Constituent(instrument_id="new", symbol="NEW"),),
            ),
        ),
    )


@pytest.fixture
def reference():
    return ReferenceBundle(
        source="synthetic calendar",
        published_at=instant(1),
        coverage_start=date(2026, 1, 2),
        coverage_end=date(2026, 1, 6),
        aliases=tuple(
            InstrumentAlias(
                instrument_id=s.lower(),
                exchange_id=s,
                symbol=s,
                valid_from=date(2026, 1, 1),
                valid_to=date(2027, 1, 1),
            )
            for s in ("OLD", "NEW")
        ),
        days=tuple(
            ExchangeDay(
                session_date=date(2026, 1, d),
                status="open",
                reason="synthetic",
                opens_at=instant(d, 4),
                closes_at=instant(d, 10),
            )
            for d in range(2, 7)
        ),
    )


def report(universe, reference, observations=(), **kwargs):
    values = dict(
        start=date(2026, 1, 2),
        end=date(2026, 1, 5),
        as_of=instant(6),
        history_sessions=1,
        future_sessions=1,
    )
    values.update(kwargs)
    return coverage_report(universe, reference, observations, **values)


def observations():
    return tuple(
        AvailableObservation(
            instrument_id=s, session_date=date(2026, 1, d), known_at=instant(d, 10)
        )
        for s in ("old", "new")
        for d in range(2, 7)
    )


def test_membership_changes_on_effective_date_and_keeps_removed_history(universe):
    assert universe.members(date(2026, 1, 3), as_of=instant(3))[0].symbol == "OLD"
    assert universe.members(date(2026, 1, 4), as_of=instant(4))[0].symbol == "NEW"
    assert universe.members(date(2026, 1, 2), as_of=instant(6))[0].symbol == "OLD"
    with pytest.raises(LookupError):
        universe.members(date(2026, 1, 1), as_of=instant(6))
    with pytest.raises(LookupError):
        universe.members(date(2026, 1, 7), as_of=instant(7))


def test_future_and_late_published_constituents_are_not_backfilled(universe, reference):
    late = universe.snapshots[0].model_copy(update={"known_at": instant(4)})
    late_universe = universe.model_copy(update={"snapshots": (late, universe.snapshots[1])})
    result = report(late_universe, reference, observations())
    assert result.rows[0].reasons == ("universe_unavailable",)
    assert result.rows[1].reasons == ("universe_unavailable",)
    assert result.rows[2].sufficient
    with pytest.raises(LookupError, match="future"):
        universe.members(date(2026, 1, 4), as_of=instant(3))


def test_coverage_uses_each_dates_universe(universe, reference):
    result = report(universe, reference, observations())
    assert [row.symbol for row in result.rows] == ["OLD", "OLD", "NEW", "NEW"]
    assert all(row.sufficient for row in result.rows)
    assert len(result.universe_version) == len(result.reference_version) == 64


def test_delisted_missing_history_is_explicit(universe, reference):
    universe = universe.model_copy(
        update={
            "limitations": (
                HistoryLimitation(
                    instrument_id="old", known_at=instant(4), reason="missing_delisted_history"
                ),
            )
        }
    )
    result = report(
        universe, reference, tuple(o for o in observations() if o.instrument_id == "new")
    )
    old = result.rows[0]
    assert not old.sufficient
    assert old.missing_history == (date(2026, 1, 2),)
    assert old.missing_future == (date(2026, 1, 3),)
    assert "missing_delisted_history" in old.reasons
    assert result.limitations == ("old: missing_delisted_history",)
    assert result.rows[-1].sufficient


def test_late_observations_do_not_satisfy_historical_features(universe, reference):
    late = tuple(item.model_copy(update={"known_at": instant(6)}) for item in observations())
    result = report(universe, reference, late)
    assert all("missing_history" in row.reasons for row in result.rows)
    assert all(not row.missing_future for row in result.rows)


def test_window_bounds_and_unavailable_calendar(universe, reference):
    result = report(universe, reference, observations(), history_sessions=2, future_sessions=2)
    assert "window_calendar_unavailable" in result.rows[0].reasons
    assert "window_calendar_unavailable" in result.rows[-1].reasons
    assert result.rows[1].sufficient
    missing = report(universe, reference, end=date(2026, 1, 7), as_of=instant(7))
    assert missing.rows[-1].reasons == ("calendar_unavailable",)


def test_closed_dates_skipped_and_incomplete_session_blocked(universe, reference):
    closed = ExchangeDay(session_date=date(2026, 1, 3), status="closed", reason="synthetic")
    reference = reference.model_copy(
        update={"days": (reference.days[0], closed, *reference.days[2:])}
    )
    result = report(universe, reference, observations())
    assert date(2026, 1, 3) not in [row.session_date for row in result.rows]
    result = report(universe, reference, observations(), end=date(2026, 1, 2), as_of=instant(2, 8))
    assert result.rows[0].reasons == ("session_not_closed",)


def test_store_versions_and_integrity(universe, tmp_path):
    source = tmp_path / "input.json"
    source.write_text(universe.model_dump_json())
    store = UniverseStore(tmp_path / "versions")
    version = store.import_file(source)
    assert store.import_file(source) == version
    assert store.get(version) == universe
    with pytest.raises(ValueError):
        store.get("../input")
    (tmp_path / "versions" / f"{version}.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        store.get(version)


def test_report_is_reproducible_and_records_configuration(universe, reference, tmp_path):
    first = report(universe, reference, observations())
    assert first.save(tmp_path) == report(universe, reference, observations()).save(tmp_path)
    assert report(universe, reference, observations(), history_sessions=2).save(
        tmp_path
    ) != first.save(tmp_path)


def test_invalid_intervals_duplicates_and_timestamps_fail(universe, reference):
    with pytest.raises(ValueError, match="Overlapping"):
        Universe(name="bad", snapshots=(universe.snapshots[0], universe.snapshots[0]))
    value = universe.snapshots[0].model_dump()
    with pytest.raises(ValueError, match="Duplicate"):
        ConstituentSnapshot(**{**value, "members": value["members"] * 2})
    with pytest.raises(ValueError, match="timezone"):
        ConstituentSnapshot(**{**value, "known_at": datetime(2026, 1, 1)})
    with pytest.raises(ValueError):
        report(universe, reference, history_sessions=0)
    with pytest.raises(ValueError):
        AvailableObservation(
            instrument_id="old", session_date=date(2026, 1, 3), known_at=instant(2)
        )
