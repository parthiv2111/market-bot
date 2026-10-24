"""Official UDiFF cash report parsing; no built-in website downloader."""

import csv
import hashlib
import io
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from prediction_bot.providers import RAW_COLUMNS, PriceBar, ProviderError, parse_csv

PARSER_VERSION = "nse-cm-udiff-2026-06-30-v1"
MAX_BYTES = 32 * 1024 * 1024
HEADERS = tuple(
    (
        "TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,"
        "FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,"
        "LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,"
        "TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4"
    ).split(",")
)


@dataclass(frozen=True)
class NSERecord:
    bar: PriceBar
    series: str
    exchange_id: str
    isin: str
    session_id: str


@dataclass(frozen=True)
class NSEReport:
    source_sha256: str
    source_date: date
    parser_version: str
    records: tuple[NSERecord, ...]
    no_trade_rows: int


def parse_report(payload: bytes, source_date: date) -> NSEReport:
    if len(payload) > MAX_BYTES:
        raise ProviderError("NSE report exceeds size limit")
    original_hash = hashlib.sha256(payload).hexdigest()
    if payload.startswith(b"PK"):
        try:
            with ZipFile(io.BytesIO(payload)) as archive:
                members = archive.infolist()
                if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
                    raise ProviderError("NSE archive must contain exactly one CSV")
                if members[0].file_size > MAX_BYTES or members[0].flag_bits & 1:
                    raise ProviderError("NSE archive is oversized or encrypted")
                payload = archive.read(members[0])
        except (BadZipFile, RuntimeError):
            raise ProviderError("Invalid NSE ZIP archive") from None
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
        if reader.fieldnames is None or tuple(reader.fieldnames) != HEADERS:
            raise ProviderError(f"Unsupported NSE schema; expected {PARSER_VERSION}")
        records = []
        no_trade_rows = 0
        seen: set[tuple[str, str, str]] = set()
        for row in reader:
            line = reader.line_num
            if None in row or any(value is None for value in row.values()):
                raise ProviderError(f"Line {line}: NSE column count mismatch")
            if row["Src"] != "NSE" or row["Sgmt"] != "CM" or row["FinInstrmTp"] != "STK":
                raise ProviderError(f"Line {line}: only NSE CM STK reports are supported")
            if row["TradDt"] != source_date.isoformat() or row["BizDt"] != source_date.isoformat():
                raise ProviderError(f"Line {line}: report source date mismatch")
            if row["SsnId"] != "F1":
                raise ProviderError(f"Line {line}: only final primary-session F1 is supported")
            if any(not row[key].strip() for key in ("TckrSymb", "SctySrs", "FinInstrmId", "ISIN")):
                raise ProviderError(f"Line {line}: missing NSE identity/series")
            key = (row["TckrSymb"], row["SctySrs"], row["SsnId"])
            if key in seen:
                raise ProviderError(f"Line {line}: duplicate symbol/series/session")
            seen.add(key)
            volume = row["TtlTradgVol"]
            if not volume.isascii() or not volume.isdecimal():
                raise ProviderError(f"Line {line}: invalid NSE traded quantity")
            if int(volume) == 0:
                no_trade_rows += 1
                continue  # Do not turn an untraded security into a fresh price observation.
            normalized = io.StringIO()
            writer = csv.writer(normalized, lineterminator="\n")
            writer.writerow(RAW_COLUMNS)
            writer.writerow(
                (
                    "NSE",
                    row["TckrSymb"],
                    row["TradDt"],
                    row["OpnPric"],
                    row["HghPric"],
                    row["LwPric"],
                    row["ClsPric"],
                    volume,
                )
            )
            try:
                bar = parse_csv(normalized.getvalue().encode())[0]
            except ProviderError:
                raise ProviderError(f"Line {line}: invalid NSE OHLCV") from None
            records.append(
                NSERecord(bar, row["SctySrs"], row["FinInstrmId"], row["ISIN"], row["SsnId"])
            )
        if not seen:
            raise ProviderError("NSE report has no rows to verify its source date")
        return NSEReport(
            original_hash,
            source_date,
            PARSER_VERSION,
            tuple(sorted(records, key=lambda item: (item.bar.symbol, item.series))),
            no_trade_rows,
        )
    except (UnicodeDecodeError, csv.Error):
        raise ProviderError("Invalid NSE CSV encoding or quoting") from None


def _publish(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ProviderError("NSE archive integrity mismatch") from None
    finally:
        temporary_path.unlink(missing_ok=True)


def import_report(payload: bytes, source_date: date, directory: Path) -> NSEReport:
    report = parse_report(payload, source_date)
    _publish(directory / f"{report.source_sha256}.source", payload)
    manifest = json.dumps(
        {
            "source_sha256": report.source_sha256,
            "source_date": source_date.isoformat(),
            "parser_version": PARSER_VERSION,
            "record_count": len(report.records),
            "no_trade_rows": report.no_trade_rows,
        },
        sort_keys=True,
    ).encode()
    _publish(directory / f"{report.source_sha256}.{PARSER_VERSION}.json", manifest)
    return report


@dataclass(frozen=True)
class FetchResponse:
    status: int
    body: bytes
    retry_after: str | None = None


class PermittedTransport(Protocol):
    def get(self, url: str) -> FetchResponse: ...


class ResumableFetchError(ProviderError):
    def __init__(self, status: int, retry_after: str | None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(
            f"Fetch paused (HTTP {status}); resume only after access/retry conditions clear"
        )


def fetch_permitted_report(
    url: str,
    *,
    permitted_urls: frozenset[str],
    transport: PermittedTransport,
    source_date: date,
    directory: Path,
) -> NSEReport:
    parsed = urlsplit(url)
    if url not in permitted_urls or parsed.scheme != "https" or parsed.username or parsed.password:
        raise ProviderError("URL is not explicitly permitted")
    try:
        response = transport.get(url)
    except (TimeoutError, ConnectionError):
        response = FetchResponse(0, b"")
    if response.status != 200:
        # Immutable attempts can be resumed without losing the initial failure record.
        failure = json.dumps(
            {
                "source_date": source_date.isoformat(),
                "status": response.status,
                "retry_after": response.retry_after,
                "parser_version": PARSER_VERSION,
            },
            sort_keys=True,
        ).encode()
        _publish(directory / "failures" / f"{hashlib.sha256(failure).hexdigest()}.json", failure)
        raise ResumableFetchError(response.status, response.retry_after)
    return import_report(response.body, source_date, directory)
