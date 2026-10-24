import csv
import io
import json
from datetime import date
from decimal import Decimal
from zipfile import ZipFile

import pytest

from prediction_bot.nse_reports import (
    HEADERS,
    PARSER_VERSION,
    FetchResponse,
    ResumableFetchError,
    fetch_permitted_report,
    import_report,
    parse_report,
)
from prediction_bot.providers import ProviderError

DAY = date(2026, 1, 2)


def fixture(**changes):
    row = dict.fromkeys(HEADERS, "")
    row.update(
        TradDt=DAY.isoformat(),
        BizDt=DAY.isoformat(),
        Sgmt="CM",
        Src="NSE",
        FinInstrmTp="STK",
        FinInstrmId="999999",
        ISIN="SYNTHETIC001",
        TckrSymb="SYNTH_ONLY",
        SctySrs="EQ",
        OpnPric="100",
        HghPric="110",
        LwPric="90",
        ClsPric="105",
        TtlTradgVol="1234",
        SsnId="F1",
    )
    row.update(changes)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue().encode()


def test_official_schema_fixture_preserves_ohlcv_series_and_identity():
    report = parse_report(fixture(SctySrs="BE"), DAY)
    record = report.records[0]
    assert record.series == "BE"
    assert record.exchange_id == "999999"
    assert record.isin == "SYNTHETIC001"
    assert record.bar.open == Decimal("100")
    assert record.bar.high == Decimal("110")
    assert record.bar.low == Decimal("90")
    assert record.bar.close == Decimal("105")
    assert record.bar.volume == 1234
    assert record.bar.adjusted_close is None


def test_zip_and_persisted_original_manifest(tmp_path):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("report.csv", fixture())
    payload = output.getvalue()
    report = import_report(payload, DAY, tmp_path)
    assert import_report(payload, DAY, tmp_path) == report
    assert (tmp_path / f"{report.source_sha256}.source").read_bytes() == payload
    manifest = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert manifest["source_date"] == DAY.isoformat()
    assert manifest["parser_version"] == PARSER_VERSION
    assert report.records == parse_report(fixture(), DAY).records


@pytest.mark.parametrize(
    "changes",
    [
        {"Sgmt": "FO"},
        {"Src": "BSE"},
        {"TradDt": "2026-01-01"},
        {"SsnId": "I1"},
        {"SsnId": "F2"},
        {"SctySrs": ""},
        {"OpnPric": "NaN"},
        {"TtlTradgVol": "-1"},
        {"FinInstrmTp": "STF"},
    ],
)
def test_unsupported_or_invalid_report_fails(changes):
    with pytest.raises(ProviderError):
        parse_report(fixture(**changes), DAY)


def test_schema_change_and_duplicate_fail():
    with pytest.raises(ProviderError, match="schema"):
        parse_report(fixture().replace(b"OpnPric", b"OpeningPrice"), DAY)
    payload = fixture()
    with pytest.raises(ProviderError, match="duplicate"):
        parse_report(payload + payload.split(b"\n")[1] + b"\n", DAY)


def test_no_trade_row_is_not_a_fresh_price():
    result = parse_report(fixture(TtlTradgVol="0", OpnPric="0"), DAY)
    assert result.records == ()
    assert result.no_trade_rows == 1


def test_unexpected_zip_contents_rejected():
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("a.csv", fixture())
        archive.writestr("b.csv", fixture())
    with pytest.raises(ProviderError, match="exactly one"):
        parse_report(output.getvalue(), DAY)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_access_failure_is_persisted_and_explicitly_resumable(tmp_path, status):
    class Transport:
        calls = 0

        def get(self, url):
            self.calls += 1
            return (
                FetchResponse(status, b"", "60")
                if self.calls == 1
                else FetchResponse(200, fixture())
            )

    transport = Transport()
    url = "https://authorized.example/report.csv"
    with pytest.raises(ResumableFetchError) as error:
        fetch_permitted_report(
            url,
            permitted_urls=frozenset({url}),
            transport=transport,
            source_date=DAY,
            directory=tmp_path,
        )
    assert error.value.status == status
    assert error.value.retry_after == "60"
    assert transport.calls == 1  # No internal retry, proxy rotation or fallback.
    assert len(list((tmp_path / "failures").glob("*.json"))) == 1
    result = fetch_permitted_report(
        url,
        permitted_urls=frozenset({url}),
        transport=transport,
        source_date=DAY,
        directory=tmp_path,
    )
    assert len(result.records) == 1


def test_unpermitted_url_never_calls_transport(tmp_path):
    class NeverCalled:
        def get(self, url):
            raise AssertionError("Must not attempt a connection")

    with pytest.raises(ProviderError, match="permitted"):
        fetch_permitted_report(
            "https://www.nseindia.com/",
            permitted_urls=frozenset(),
            transport=NeverCalled(),
            source_date=DAY,
            directory=tmp_path,
        )
