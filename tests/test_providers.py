from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from prediction_bot.providers import (
    CSVProvider,
    DailyProvider,
    FetchRequest,
    ProviderError,
    export_csv,
    parse_csv,
)

HEADER = "exchange,symbol,session_date,open,high,low,close,volume\n"
ROWS = "NSE,SYNTH_A,2026-01-02,100,110,90,105,1000\nNSE,SYNTH_A,2026-01-05,105,115,100,110,2000\n"


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "synthetic.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    return path


def request():
    return FetchRequest(
        ("SYNTH_A", "MISSING"),
        date(2026, 1, 2),
        date(2026, 1, 6),
        (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)),
    )


def test_deterministic_roundtrip_and_provenance(source):
    provider = CSVProvider(
        source, usage_basis="synthetic", fetched_at=datetime(2026, 1, 7, tzinfo=UTC)
    )
    result = provider.fetch(request())
    assert provider.fetch(request()) == result
    encoded = export_csv(result.bars)
    assert parse_csv(encoded) == result.bars
    assert export_csv(parse_csv(encoded)) == encoded
    assert result.bars[0].close == Decimal("105")
    assert result.bars[0].adjusted_close is None
    assert result.provenance.adjustment_policy == "raw_only"
    assert result.provenance.source_name == "synthetic.csv"
    assert len(result.provenance.source_sha256) == 64
    assert result.source_row_count == 2
    assert result.coverage[0].missing_sessions == (date(2026, 1, 6),)
    assert result.coverage[1].observed_sessions == ()
    assert len(result.coverage[1].missing_sessions) == 3


def test_provider_replaceable_without_consumer_changes(source):
    original = CSVProvider(source, usage_basis="synthetic").fetch(request())

    class FixtureProvider:
        def fetch(self, query):
            assert query == request()
            return original

    def consume(provider: DailyProvider):
        return tuple(bar.close for bar in provider.fetch(request()).bars)

    assert consume(FixtureProvider()) == consume(CSVProvider(source, usage_basis="synthetic"))


@pytest.mark.parametrize(
    "payload,diagnostic",
    [
        ("", "columns"),
        (HEADER.replace("volume", "close") + ROWS, "Duplicate"),
        (HEADER + "NSE,A,2026-01-02,1\n", "Line 2"),
        (HEADER + ROWS.replace(",100,110,90,105,", ",NaN,110,90,105,"), "positive"),
        (HEADER + ROWS.replace(",100,110,90,105,", ",120,110,90,105,"), "OHLC"),
        (HEADER + ROWS.replace(",1000", ",-1"), "volume"),
        (HEADER + ROWS.replace(",1000", ",1.2"), "volume"),
        (HEADER + ROWS.replace("2026-01-02", "20260102"), "YYYY-MM-DD"),
        (HEADER + ROWS + ROWS.splitlines()[0] + "\n", "duplicate"),
        (HEADER + ROWS.replace("NSE", "BSE"), "NSE"),
    ],
)
def test_malformed_inputs_are_rejected(payload, diagnostic):
    with pytest.raises(ProviderError, match=diagnostic):
        parse_csv(payload.encode())


def test_adjusted_prices_remain_separate():
    payload = (
        HEADER.strip()
        + ",adjusted_open,adjusted_high,adjusted_low,adjusted_close\n"
        + "NSE,SYNTH,2026-01-02,100,110,90,105,1000,50,55,45,52.5\n"
    ).encode()
    bar = parse_csv(payload)[0]
    assert bar.close == Decimal("105")
    assert bar.adjusted_close == Decimal("52.5")
    assert parse_csv(export_csv((bar,))) == (bar,)
    with pytest.raises(ProviderError, match="adjusted"):
        parse_csv(payload.replace(b",50,55,45,52.5", b",50,,45,52.5"))


def test_empty_file_and_unknown_calendar_do_not_fabricate_coverage(source):
    source.write_text(HEADER)
    result = CSVProvider(source, usage_basis="synthetic").fetch(
        FetchRequest(("A",), date(2026, 1, 1), date(2026, 1, 2))
    )
    assert result.bars == ()
    assert result.coverage[0].missing_sessions is None


def test_unexpected_sessions_and_filtering(source):
    query = FetchRequest(("SYNTH_A",), date(2026, 1, 2), date(2026, 1, 5), (date(2026, 1, 2),))
    result = CSVProvider(source, usage_basis="synthetic").fetch(query)
    assert result.coverage[0].unexpected_sessions == (date(2026, 1, 5),)
    narrow = FetchRequest(("SYNTH_A",), date(2026, 1, 5), date(2026, 1, 5))
    assert len(CSVProvider(source, usage_basis="synthetic").fetch(narrow).bars) == 1


def test_invalid_request_and_manifest(source):
    with pytest.raises(ValueError):
        FetchRequest((), date(2026, 1, 2), date(2026, 1, 1))
    with pytest.raises(ValueError):
        CSVProvider(source, usage_basis="")
    with pytest.raises(ValueError):
        CSVProvider(source, usage_basis="synthetic", fetched_at=datetime(2026, 1, 1))
    source.unlink()
    with pytest.raises(ProviderError, match="unreadable"):
        CSVProvider(source, usage_basis="synthetic").fetch(request())
