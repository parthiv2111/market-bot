"""Optional, explicitly enabled Yahoo daily research adapter. Never an execution feed."""

import hashlib
import importlib
import io
import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from prediction_bot.providers import (
    ADJUSTED_COLUMNS,
    RAW_COLUMNS,
    CSVProvider,
    FetchRequest,
    Provenance,
    ProviderError,
    ProviderResult,
    parse_csv,
)


def _download(ticker: str, start: str, end: str) -> Any:
    try:
        yahoo = importlib.import_module("yfinance")
    except ModuleNotFoundError:
        raise ProviderError("Install the optional yahoo dependency group") from None
    return yahoo.download(
        ticker,
        start=start,
        end=end,
        interval="1d",
        auto_adjust=False,
        back_adjust=False,
        actions=True,
        repair=False,
        keepna=True,
        threads=False,
        progress=False,
        timeout=10,
        multi_level_index=False,
    )


class YahooResearchProvider:
    def __init__(
        self,
        cache_directory: Path,
        ticker_map: Mapping[str, str],
        *,
        enabled: bool = False,
        usage_basis: str = "",
        ttl_seconds: int = 86400,
        attempts: int = 2,
        downloader: Callable[[str, str, str], Any] = _download,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not enabled or not usage_basis.strip():
            raise ProviderError(
                "Yahoo research access requires explicit enablement and usage basis"
            )
        if attempts not in (1, 2, 3) or ttl_seconds <= 0:
            raise ValueError("Require 1–3 attempts and a positive cache lifetime")
        if any(not key.strip() or not value.endswith(".NS") for key, value in ticker_map.items()):
            raise ValueError("Provide explicit NSE symbol-to-.NS ticker mappings")
        if len(set(ticker_map.values())) != len(ticker_map):
            raise ValueError("Ticker mappings must be unambiguous")
        self.cache_directory = cache_directory
        self.ticker_map = dict(ticker_map)
        self.usage_basis = usage_basis
        self.ttl_seconds = ttl_seconds
        self.attempts = attempts
        self.downloader = downloader
        self.clock = clock
        self.sleep = sleep

    def fetch(self, request: FetchRequest) -> ProviderResult:
        import csv

        if any(symbol not in self.ticker_map for symbol in request.symbols):
            raise ProviderError("Missing explicit NSE ticker mapping")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must be timezone-aware")
        identity = json.dumps(
            {
                "version": 1,
                "tickers": [(s, self.ticker_map[s]) for s in request.symbols],
                "start": request.start.isoformat(),
                "end": request.end.isoformat(),
                "interval": "1d",
                "auto_adjust": False,
                "back_adjust": False,
                "repair": False,
                "actions": True,
            },
            sort_keys=True,
        )
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cache_file = self.cache_directory / f"{key}.json"
        payload: bytes | None = None
        fetched_at = now
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached_at = datetime.fromisoformat(cached["fetched_at"])
                candidate = cached["csv"].encode("utf-8")
                if (
                    cached_at.tzinfo is None
                    or cached["request"] != identity
                    or hashlib.sha256(candidate).hexdigest() != cached["sha256"]
                ):
                    raise ValueError("invalid cache")
                if timedelta(0) <= now - cached_at < timedelta(seconds=self.ttl_seconds):
                    parse_csv(candidate)
                    payload, fetched_at = candidate, cached_at
            except (KeyError, TypeError, ValueError, OSError):
                raise ProviderError(
                    "Research cache is invalid; remove it explicitly before refetch"
                ) from None
        if payload is None:
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow((*RAW_COLUMNS, *ADJUSTED_COLUMNS))
            for symbol in request.symbols:
                frame = None
                for attempt in range(self.attempts):
                    try:
                        frame = self.downloader(
                            self.ticker_map[symbol],
                            request.start.isoformat(),
                            (request.end + timedelta(days=1)).isoformat(),
                        )
                        break
                    except (TimeoutError, ConnectionError):
                        if attempt + 1 == self.attempts:
                            raise ProviderError(
                                "Yahoo transport failed after bounded retries"
                            ) from None
                        self.sleep(float(2**attempt))
                    except Exception:
                        # Access and library failures are not retried or bypassed.
                        raise ProviderError(
                            "Yahoo access failed; review source limits before resuming"
                        ) from None
                if frame is None:
                    raise ProviderError("Yahoo returned no response")
                if frame.empty:
                    continue  # Coverage explicitly reports missing dates/symbols.
                required = {"Open", "High", "Low", "Close", "Adj Close", "Volume"}
                if not required.issubset(frame.columns):
                    raise ProviderError("Yahoo response schema changed")
                for timestamp, row in frame.iterrows():
                    day = timestamp.date()
                    if not request.start <= day <= request.end:
                        raise ProviderError("Yahoo response date lies outside requested coverage")
                    try:
                        opening, high, low, close, adjusted_close, volume = (
                            Decimal(str(row[column]))
                            for column in ("Open", "High", "Low", "Close", "Adj Close", "Volume")
                        )
                        if not all(
                            value.is_finite()
                            for value in (opening, high, low, close, adjusted_close, volume)
                        ):
                            raise ValueError("nonfinite")
                        if (
                            close <= 0
                            or adjusted_close <= 0
                            or volume < 0
                            or volume != volume.to_integral_value()
                        ):
                            raise ValueError("invalid prices/volume")
                    except (ValueError, TypeError, InvalidOperation):
                        raise ProviderError(
                            "Yahoo returned invalid OHLCV or adjustment values"
                        ) from None
                    factor = adjusted_close / close
                    writer.writerow(
                        (
                            "NSE",
                            symbol,
                            day.isoformat(),
                            opening,
                            high,
                            low,
                            close,
                            int(volume),
                            opening * factor,
                            high * factor,
                            low * factor,
                            adjusted_close,
                        )
                    )
            payload = buffer.getvalue().encode("utf-8")
            parse_csv(payload)
            encoded = json.dumps(
                {
                    "request": identity,
                    "fetched_at": now.isoformat(),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "csv": payload.decode("utf-8"),
                },
                sort_keys=True,
            ).encode()
            with NamedTemporaryFile(dir=self.cache_directory, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            try:
                os.replace(temporary_path, cache_file)
            finally:
                temporary_path.unlink(missing_ok=True)
        # Reuse the same validated provider result/coverage contract as local CSV.
        with NamedTemporaryFile(dir=self.cache_directory, suffix=".csv", delete=False) as temporary:
            path = Path(temporary.name)
            temporary.write(payload)
        try:
            result = CSVProvider(path, usage_basis=self.usage_basis, fetched_at=fetched_at).fetch(
                request
            )
        finally:
            path.unlink(missing_ok=True)
        return ProviderResult(
            request,
            result.bars,
            Provenance(
                "yfinance",
                key,
                hashlib.sha256(payload).hexdigest(),
                fetched_at.astimezone(UTC),
                self.usage_basis,
                "supplied_adjusted",
            ),
            result.coverage,
            result.source_row_count,
        )
