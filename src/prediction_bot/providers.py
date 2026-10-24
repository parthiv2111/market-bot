"""Provider-independent daily bars and deterministic local CSV ingestion."""

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol


class ProviderError(ValueError):
    """A safe schema/row diagnostic that does not echo raw file contents."""


@dataclass(frozen=True)
class PriceBar:
    exchange: str
    symbol: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_open: Decimal | None = None
    adjusted_high: Decimal | None = None
    adjusted_low: Decimal | None = None
    adjusted_close: Decimal | None = None


@dataclass(frozen=True)
class FetchRequest:
    symbols: tuple[str, ...]
    start: date
    end: date  # Inclusive session dates.
    expected_sessions: tuple[date, ...] | None = None

    def __post_init__(self) -> None:
        if self.end < self.start or not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("Require a valid date range and distinct nonempty symbols")
        if any(not symbol.strip() for symbol in self.symbols):
            raise ValueError("Symbols cannot be blank")
        if self.expected_sessions is not None:
            if len(set(self.expected_sessions)) != len(self.expected_sessions):
                raise ValueError("Expected sessions must be distinct")
            if any(not self.start <= day <= self.end for day in self.expected_sessions):
                raise ValueError("Expected sessions must lie inside the request")


@dataclass(frozen=True)
class Provenance:
    provider: str
    source_name: str
    source_sha256: str
    fetched_at: datetime
    usage_basis: str
    adjustment_policy: Literal["raw_only", "supplied_adjusted"]


@dataclass(frozen=True)
class SymbolCoverage:
    symbol: str
    observed_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...] | None
    unexpected_sessions: tuple[date, ...] | None


@dataclass(frozen=True)
class ProviderResult:
    request: FetchRequest
    bars: tuple[PriceBar, ...]
    provenance: Provenance
    coverage: tuple[SymbolCoverage, ...]
    source_row_count: int


class DailyProvider(Protocol):
    def fetch(self, request: FetchRequest) -> ProviderResult: ...


RAW_COLUMNS = ("exchange", "symbol", "session_date", "open", "high", "low", "close", "volume")
ADJUSTED_COLUMNS = ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close")


def _price(value: str, column: str, line: int) -> Decimal:
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ProviderError(f"Line {line}: invalid {column}") from None
    if not result.is_finite() or result <= 0:
        raise ProviderError(f"Line {line}: {column} must be finite and positive")
    return result


def parse_csv(payload: bytes) -> tuple[PriceBar, ...]:
    try:
        reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
        header = next(reader, [])
        if len(header) != len(set(header)):
            raise ProviderError("Duplicate CSV columns")
        expected = set(RAW_COLUMNS)
        if set(header) not in (expected, expected | set(ADJUSTED_COLUMNS)):
            raise ProviderError("Expected raw columns and optionally all four adjusted columns")
        adjusted = set(ADJUSTED_COLUMNS).issubset(header)
        bars = []
        identities: set[tuple[str, str, date]] = set()
        for cells in reader:
            line = reader.line_num
            if len(cells) != len(header):
                raise ProviderError(f"Line {line}: column count does not match header")
            row = dict(zip(header, cells, strict=True))
            if row["exchange"] != "NSE" or not row["symbol"].strip():
                raise ProviderError(f"Line {line}: require NSE and a nonempty symbol")
            if row["symbol"] != row["symbol"].strip():
                raise ProviderError(f"Line {line}: symbol contains surrounding whitespace")
            try:
                session = date.fromisoformat(row["session_date"])
            except ValueError:
                raise ProviderError(f"Line {line}: session_date must be YYYY-MM-DD") from None
            if session.isoformat() != row["session_date"]:
                raise ProviderError(f"Line {line}: session_date must be YYYY-MM-DD")
            volume_text = row["volume"]
            if not volume_text.isascii() or not volume_text.isdecimal():
                raise ProviderError(f"Line {line}: volume must be a nonnegative integer")
            prices = [_price(row[column], column, line) for column in RAW_COLUMNS[3:7]]
            adjusted_prices: list[Decimal | None] = [None] * 4
            if adjusted:
                values = [row[column] for column in ADJUSTED_COLUMNS]
                if any(values):
                    adjusted_prices = [
                        _price(row[column], column, line) for column in ADJUSTED_COLUMNS
                    ]
            for group in (prices, adjusted_prices):
                opening, high, low, close = group
                if (
                    opening is not None
                    and high is not None
                    and low is not None
                    and close is not None
                ):
                    if not low <= opening <= high or not low <= close <= high:
                        raise ProviderError(f"Line {line}: inconsistent OHLC prices")
            identity = (row["exchange"], row["symbol"], session)
            if identity in identities:
                raise ProviderError(f"Line {line}: duplicate exchange/symbol/session")
            identities.add(identity)
            bars.append(
                PriceBar(
                    row["exchange"],
                    row["symbol"],
                    session,
                    prices[0],
                    prices[1],
                    prices[2],
                    prices[3],
                    int(volume_text),
                    adjusted_prices[0],
                    adjusted_prices[1],
                    adjusted_prices[2],
                    adjusted_prices[3],
                )
            )
        return tuple(sorted(bars, key=lambda bar: (bar.exchange, bar.symbol, bar.session_date)))
    except (UnicodeDecodeError, csv.Error):
        raise ProviderError("Invalid UTF-8 CSV encoding or quoting") from None


def export_csv(bars: tuple[PriceBar, ...]) -> bytes:
    """Canonical row order, UTF-8 and LF; absent adjustments remain empty."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow((*RAW_COLUMNS, *ADJUSTED_COLUMNS))
    for bar in sorted(bars, key=lambda item: (item.exchange, item.symbol, item.session_date)):
        writer.writerow(
            (
                bar.exchange,
                bar.symbol,
                bar.session_date.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                *(
                    value if value is not None else ""
                    for value in (
                        bar.adjusted_open,
                        bar.adjusted_high,
                        bar.adjusted_low,
                        bar.adjusted_close,
                    )
                ),
            )
        )
    payload = output.getvalue().encode("utf-8")
    parse_csv(payload)  # Apply the same validation to programmatically constructed bars.
    return payload


class CSVProvider:
    def __init__(self, path: Path, *, usage_basis: str, fetched_at: datetime | None = None):
        if not usage_basis.strip():
            raise ValueError("A permitted-use basis is required")
        if fetched_at is not None and (fetched_at.tzinfo is None or fetched_at.utcoffset() is None):
            raise ValueError("Fetch timestamp must be timezone-aware")
        self.path = path
        self.usage_basis = usage_basis
        self.fetched_at = fetched_at

    def fetch(self, request: FetchRequest) -> ProviderResult:
        try:
            payload = self.path.read_bytes()
        except OSError:
            raise ProviderError("Local CSV is unreadable") from None
        all_bars = parse_csv(payload)
        bars = tuple(
            bar
            for bar in all_bars
            if bar.symbol in request.symbols and request.start <= bar.session_date <= request.end
        )
        coverage = []
        for symbol in request.symbols:
            observed = tuple(bar.session_date for bar in bars if bar.symbol == symbol)
            expected = request.expected_sessions
            coverage.append(
                SymbolCoverage(
                    symbol,
                    observed,
                    None if expected is None else tuple(sorted(set(expected) - set(observed))),
                    None if expected is None else tuple(sorted(set(observed) - set(expected))),
                )
            )
        fetched_at = self.fetched_at or datetime.now(UTC)
        return ProviderResult(
            request,
            bars,
            Provenance(
                "local_csv",
                self.path.name,
                hashlib.sha256(payload).hexdigest(),
                fetched_at.astimezone(UTC),
                self.usage_basis,
                "supplied_adjusted"
                if any(bar.adjusted_close is not None for bar in all_bars)
                else "raw_only",
            ),
            tuple(coverage),
            len(all_bars),
        )
