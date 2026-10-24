from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from prediction_bot.providers import FetchRequest, ProviderError
from prediction_bot.yahoo_provider import YahooResearchProvider, _download


def frame():
    return pd.DataFrame(
        {
            "Open": [100],
            "High": [110],
            "Low": [90],
            "Close": [105],
            "Adj Close": [52.5],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-01-02"]),
    )


def query():
    return FetchRequest(("SYNTH",), date(2026, 1, 2), date(2026, 1, 2), (date(2026, 1, 2),))


def provider(tmp_path, download, **kwargs):
    return YahooResearchProvider(
        tmp_path,
        {"SYNTH": "SYNTH.NS"},
        enabled=True,
        usage_basis="mock-only",
        downloader=download,
        **kwargs,
    )


def test_default_disabled_and_mapping_required(tmp_path):
    with pytest.raises(ProviderError, match="enablement"):
        YahooResearchProvider(tmp_path, {})
    with pytest.raises(ValueError, match="mapping"):
        YahooResearchProvider(tmp_path, {"A": "A.US"}, enabled=True, usage_basis="mock")
    with pytest.raises(ProviderError, match="mapping"):
        provider(tmp_path, lambda *args: frame()).fetch(
            FetchRequest(("UNKNOWN",), date(2026, 1, 2), date(2026, 1, 2))
        )


def test_raw_adjusted_semantics_cache_and_expiry(tmp_path):
    calls = []
    current = [datetime(2026, 1, 3, tzinfo=UTC)]

    def download(*args):
        calls.append(args)
        return frame()

    adapter = provider(tmp_path, download, clock=lambda: current[0], ttl_seconds=60)
    first = adapter.fetch(query())
    assert first.bars[0].close == Decimal("105")
    assert first.bars[0].adjusted_close == Decimal("52.5")
    assert first.bars[0].adjusted_open == Decimal("50")
    assert first.provenance.provider == "yfinance"
    assert first.coverage[0].missing_sessions == ()
    assert adapter.fetch(query()) == first
    assert calls == [("SYNTH.NS", "2026-01-02", "2026-01-03")]
    current[0] += timedelta(seconds=60)
    assert adapter.fetch(query()).provenance.fetched_at == current[0]
    assert len(calls) == 2


def test_downloader_flags_are_explicit(monkeypatch):
    calls = []
    fake = SimpleNamespace(download=lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr("prediction_bot.yahoo_provider.importlib.import_module", lambda _: fake)
    _download("SYNTH.NS", "2026-01-02", "2026-01-03")
    flags = calls[0][1]
    assert flags["auto_adjust"] is False
    assert flags["back_adjust"] is False
    assert flags["repair"] is False
    assert flags["threads"] is False
    assert flags["actions"] is True
    assert flags["interval"] == "1d"
    assert flags["timeout"] == 10


def test_bounded_timeout_retries_and_no_access_error_retries(tmp_path):
    calls, sleeps = [], []

    def timeout(*args):
        calls.append(args)
        raise TimeoutError()

    with pytest.raises(ProviderError, match="bounded"):
        provider(tmp_path, timeout, attempts=3, sleep=sleeps.append).fetch(query())
    assert len(calls) == 3
    assert sleeps == [1.0, 2.0]
    calls.clear()

    def denied(*args):
        calls.append(args)
        raise RuntimeError("rate limited")

    with pytest.raises(ProviderError, match="access"):
        provider(tmp_path, denied, attempts=3, sleep=sleeps.append).fetch(query())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "column,value", [("Close", 0), ("High", float("nan")), ("Volume", 0.5), ("Adj Close", -1)]
)
def test_invalid_response_rejected(tmp_path, column, value):
    response = frame().astype(float)
    response.loc[response.index[0], column] = value
    with pytest.raises(ProviderError):
        provider(tmp_path, lambda *args: response).fetch(query())
    assert not list(tmp_path.glob("*.json"))


def test_empty_response_is_missing_and_corrupt_cache_fails(tmp_path):
    adapter = provider(tmp_path, lambda *args: pd.DataFrame())
    result = adapter.fetch(query())
    assert result.bars == ()
    assert result.coverage[0].missing_sessions == (date(2026, 1, 2),)
    next(tmp_path.glob("*.json")).write_text("corrupted")
    with pytest.raises(ProviderError, match="cache"):
        adapter.fetch(query())


def test_schema_changes_fail(tmp_path):
    with pytest.raises(ProviderError, match="schema"):
        provider(tmp_path, lambda *args: frame().drop(columns=["Adj Close"])).fetch(query())
