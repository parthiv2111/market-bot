import json
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from prediction_bot.data_quality import CorporateAction, QualityStore
from prediction_bot.providers import FetchRequest

HEADER = b"exchange,symbol,session_date,open,high,low,close,volume\n"
ROW = b"NSE,SYNTH,2026-01-02,100,110,90,100,1000\n"
KNOWN = datetime(2026, 1, 3, tzinfo=UTC)
REQUEST = FetchRequest(
    ("SYNTH",), date(2026, 1, 2), date(2026, 1, 5), (date(2026, 1, 2), date(2026, 1, 5))
)


def ingest(store, payload=HEADER + ROW, known_at=KNOWN):
    return store.import_csv(payload, REQUEST, known_at=known_at, usage_basis="synthetic")


def action(**changes):
    values = dict(
        action_id="split-1",
        symbol="SYNTH",
        kind="split",
        effective_at=datetime(2026, 1, 5, tzinfo=UTC),
        known_at=KNOWN,
        source="synthetic action notice",
        price_factor=Decimal("0.5"),
        new_shares_per_old=Decimal(2),
    )
    values.update(changes)
    return CorporateAction(**values)


def test_duplicate_import_is_durable_and_idempotent(tmp_path):
    first = ingest(QualityStore(tmp_path))
    second = ingest(QualityStore(tmp_path))
    assert first == second
    assert len(list((tmp_path / "raw").iterdir())) == 1
    assert len(list((tmp_path / "validation").iterdir())) == 1
    assert next((tmp_path / "raw").iterdir()).read_bytes() == HEADER + ROW
    assert first.missing_sessions == (("SYNTH", "2026-01-05"),)
    report = json.loads(next((tmp_path / "validation").iterdir()).read_bytes())
    assert report["source_sha256"] == first.source_sha256
    assert report["raw_bars"][0]["close"] == "100"


@pytest.mark.parametrize(
    "bad",
    [
        ROW.replace(b",100,110", b",-1,110"),
        ROW.replace(b",100,110", b",NaN,110"),
        ROW.replace(b",100,110", b",120,110"),
        ROW.replace(b",1000", b",-1"),
        ROW.replace(b",1000", b",1.5"),
        ROW.replace(b"2026-01-02", b"2026-01-99"),
        ROW.replace(b"2026-01-02", b"2026-01-04"),
        ROW.replace(b"SYNTH", b"OTHER"),
    ],
)
def test_bad_rows_quarantined_with_original_cells(tmp_path, bad):
    batch = ingest(QualityStore(tmp_path), HEADER + ROW + bad)
    assert len(batch.bars) == 1
    assert len(batch.quarantine) == 1
    assert batch.quarantine[0].line == 3
    assert batch.quarantine[0].cells == tuple(bad.decode().strip().split(","))
    assert next((tmp_path / "raw").iterdir()).read_bytes() == HEADER + ROW + bad


@pytest.mark.parametrize("other", [ROW, ROW.replace(b",1000", b",2000")])
def test_all_duplicate_identities_quarantined(tmp_path, other):
    batch = ingest(QualityStore(tmp_path), HEADER + ROW + other)
    assert batch.bars == ()
    assert len(batch.quarantine) == 2
    assert len(batch.missing_sessions) == 2


@pytest.mark.parametrize("payload", [b"\xff", b"bad,header\n1,2\n", HEADER + ROW + b'"broken'])
def test_malformed_file_preserved_and_no_partial_acceptance(tmp_path, payload):
    batch = ingest(QualityStore(tmp_path), payload)
    assert batch.bars == ()
    assert batch.quarantine[-1].line == 0
    assert next((tmp_path / "raw").iterdir()).read_bytes() == payload


def test_split_and_dividend_are_separate_from_raw(tmp_path):
    store = QualityStore(tmp_path)
    batch = ingest(store)
    dividend = action(
        action_id="dividend-1",
        kind="cash_dividend",
        price_factor=Decimal("0.98"),
        new_shares_per_old=Decimal(1),
    )
    result = store.research_series(
        batch, (action(), dividend), as_of=datetime(2026, 1, 6, tzinfo=UTC)
    )
    assert result[0].adjusted_close == Decimal(49)
    assert result[0].adjusted_high == Decimal("53.9")
    assert result[0].close == batch.bars[0].close == 100
    assert result[0].volume == 1000  # Volume remains raw and explicitly labeled.
    assert batch.bars[0].adjusted_close is None
    assert len(list((tmp_path / "actions").iterdir())) == 1
    assert len(list((tmp_path / "research").iterdir())) == 1


def test_future_and_late_known_actions_cannot_change_earlier_features(tmp_path):
    store = QualityStore(tmp_path)
    batch = ingest(store)
    early = store.research_series(batch, (action(),), as_of=KNOWN)
    assert early[0].adjusted_close == 100
    late = action(known_at=datetime(2026, 1, 7, tzinfo=UTC))
    assert (
        store.research_series(batch, (late,), as_of=datetime(2026, 1, 6, tzinfo=UTC))[
            0
        ].adjusted_close
        == 100
    )
    assert (
        store.research_series(batch, (late,), as_of=datetime(2026, 1, 7, tzinfo=UTC))[
            0
        ].adjusted_close
        == 50
    )
    with pytest.raises(ValueError, match="not known"):
        store.research_series(batch, (), as_of=datetime(2026, 1, 2, tzinfo=UTC))


def test_action_never_adjusts_ex_date_or_other_symbol(tmp_path):
    store = QualityStore(tmp_path)
    batch = ingest(
        store,
        HEADER + ROW + ROW.replace(b"2026-01-02", b"2026-01-05"),
        known_at=datetime(2026, 1, 6, tzinfo=UTC),
    )
    result = store.research_series(batch, (action(),), as_of=batch.known_at)
    assert [bar.adjusted_close for bar in result] == [50, 100]
    assert (
        store.research_series(batch, (action(symbol="OTHER"),), as_of=batch.known_at)[
            0
        ].adjusted_close
        == 100
    )


def test_vendor_adjustments_are_not_used_for_point_in_time_features(tmp_path):
    store = QualityStore(tmp_path)
    batch = ingest(store)
    vendor = replace(batch, bars=(replace(batch.bars[0], adjusted_close=Decimal(1)),))
    assert store.research_series(vendor, (), as_of=KNOWN)[0].adjusted_close == 100


def test_input_guards(tmp_path):
    store = QualityStore(tmp_path)
    with pytest.raises(ValueError, match="timezone"):
        ingest(store, known_at=KNOWN.replace(tzinfo=None))
    with pytest.raises(ValueError, match="expected sessions"):
        store.import_csv(
            HEADER + ROW,
            replace(REQUEST, expected_sessions=None),
            known_at=KNOWN,
            usage_basis="synthetic",
        )
    with pytest.raises(ValueError, match="reciprocal"):
        action(price_factor=Decimal("0.3"))
    with pytest.raises(ValueError, match="Cash dividends"):
        action(kind="cash_dividend")
    with pytest.raises(ValueError, match="Duplicate action"):
        store.research_series(ingest(store), (action(), action()), as_of=KNOWN)
    assert ingest(store, known_at=datetime(2026, 1, 2, tzinfo=UTC)).bars == ()


def test_corrupt_stored_source_fails_closed(tmp_path):
    store = QualityStore(tmp_path)
    ingest(store)
    next((tmp_path / "raw").iterdir()).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        ingest(store)
