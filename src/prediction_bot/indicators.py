"""Deterministic raw-price indicators at an explicit historical knowledge cutoff."""

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import pstdev

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware
from prediction_bot.providers import PriceBar, export_csv
from prediction_bot.reference_data import FrozenModel, ReferenceBundle


class IndicatorConfig(FrozenModel):
    return_period: int = Field(default=1, ge=1, le=5000)
    sma_period: int = Field(default=20, ge=1, le=5000)
    ema_period: int = Field(default=20, ge=1, le=5000)
    rsi_period: int = Field(default=14, ge=1, le=5000)
    macd_fast: int = Field(default=12, ge=1, le=5000)
    macd_slow: int = Field(default=26, ge=2, le=5000)
    macd_signal: int = Field(default=9, ge=1, le=5000)
    atr_period: int = Field(default=14, ge=1, le=5000)
    volatility_period: int = Field(default=20, ge=2, le=5000)
    volume_period: int = Field(default=20, ge=1, le=5000)

    @model_validator(mode="after")
    def validate_macd(self) -> "IndicatorConfig":
        if self.macd_fast >= self.macd_slow:
            raise ValueError("MACD fast period must be shorter than slow period")
        return self


@dataclass(frozen=True)
class KnownBar:
    bar: PriceBar
    known_at: datetime


@dataclass(frozen=True)
class Feature:
    value: float | None
    invalid_reason: str | None = None


@dataclass(frozen=True)
class FeatureVector:
    symbol: str
    session_date: date
    as_of: datetime
    config: IndicatorConfig
    values: dict[str, Feature]
    price_basis: str = "raw"
    version: str = "indicators-v1"


NAMES = (
    "return",
    "sma",
    "ema",
    "rsi",
    "macd",
    "macd_signal",
    "macd_histogram",
    "atr",
    "volatility",
    "volume_sma",
    "relative_volume",
)


def _smooth(values: list[float], period: int, alpha: float) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    if len(values) >= period:
        current = math.fsum(values[:period]) / period
        output[period - 1] = current
        for index in range(period, len(values)):
            current += alpha * (values[index] - current)
            output[index] = current
    return output


def features_at(
    observations: tuple[KnownBar, ...],
    reference: ReferenceBundle,
    *,
    symbol: str,
    session_date: date,
    as_of: datetime,
    config: IndicatorConfig | None = None,
) -> FeatureVector:
    _aware(as_of)
    config = config or IndicatorConfig()

    def invalid(reason: str) -> FeatureVector:
        return FeatureVector(
            symbol, session_date, as_of, config, {name: Feature(None, reason) for name in NAMES}
        )

    try:
        target = reference.day(session_date)
    except LookupError:
        return invalid("calendar_unavailable")
    if target.closes_at is None:
        return invalid("not_a_session")
    if target.closes_at > as_of:
        return invalid("incomplete_session")
    selected = []
    for observation in observations:
        bar = observation.bar
        # Future rows are excluded before validation or any calculation.
        if bar.symbol != symbol or bar.session_date > session_date:
            continue
        _aware(observation.known_at)
        if observation.known_at <= as_of:
            try:
                source_session = reference.day(bar.session_date)
            except LookupError:
                return invalid("calendar_unavailable")
            if source_session.closes_at is None:
                return invalid("unexpected_session")
            if observation.known_at < source_session.closes_at:
                return invalid("invalid_knowledge_time")
            selected.append(bar)
    if not any(bar.session_date == session_date for bar in selected):
        return invalid("missing_bar")
    export_csv(tuple(selected))  # Same positive, finite OHLCV and duplicate checks as providers.
    selected.sort(key=lambda bar: bar.session_date)
    # A missing exchange session restarts all warmups; do not bridge gaps.
    segment: list[PriceBar] = []
    previous: date | None = None
    for bar in selected:
        if previous is not None:
            candidate = previous + timedelta(days=1)
            while candidate < bar.session_date:
                try:
                    if reference.day(candidate).status == "open":
                        segment.clear()
                except LookupError:
                    return invalid("calendar_unavailable")
                candidate += timedelta(days=1)
        segment.append(bar)
        previous = bar.session_date
    closes = [float(bar.close) for bar in segment]
    highs = [float(bar.high) for bar in segment]
    lows = [float(bar.low) for bar in segment]
    try:
        volumes = [float(bar.volume) for bar in segment]
    except OverflowError:
        return invalid("numeric_overflow")
    if not all(
        math.isfinite(value) and abs(value) <= 1e100
        for group in (closes, highs, lows, volumes)
        for value in group
    ) or any(value < 1e-100 for group in (closes, highs, lows) for value in group):
        return invalid("numeric_overflow")
    values = {name: Feature(None, "insufficient_history") for name in NAMES}

    def put(name: str, value: float | None) -> None:
        if value is not None:
            values[name] = (
                Feature(value) if math.isfinite(value) else Feature(None, "numeric_overflow")
            )

    count = len(closes)
    if count > config.return_period:
        put("return", closes[-1] / closes[-1 - config.return_period] - 1)
    if count >= config.sma_period:
        put("sma", math.fsum(closes[-config.sma_period :]) / config.sma_period)
    put("ema", _smooth(closes, config.ema_period, 2 / (config.ema_period + 1))[-1])
    changes = [right - left for left, right in zip(closes, closes[1:], strict=False)]
    gains = _smooth(
        [max(change, 0) for change in changes], config.rsi_period, 1 / config.rsi_period
    )
    losses = _smooth(
        [max(-change, 0) for change in changes], config.rsi_period, 1 / config.rsi_period
    )
    if gains and losses and gains[-1] is not None and losses[-1] is not None:
        gain, loss = gains[-1], losses[-1]
        put("rsi", 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss))
    fast = _smooth(closes, config.macd_fast, 2 / (config.macd_fast + 1))
    slow = _smooth(closes, config.macd_slow, 2 / (config.macd_slow + 1))
    macd = [a - b for a, b in zip(fast, slow, strict=True) if a is not None and b is not None]
    if macd:
        put("macd", macd[-1])
        signal = _smooth(macd, config.macd_signal, 2 / (config.macd_signal + 1))[-1]
        put("macd_signal", signal)
        if signal is not None:
            put("macd_histogram", macd[-1] - signal)
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, count)
    ]
    if true_ranges:
        put("atr", _smooth(true_ranges, config.atr_period, 1 / config.atr_period)[-1])
    returns = [right / left - 1 for left, right in zip(closes, closes[1:], strict=False)]
    if len(returns) >= config.volatility_period:
        window = returns[-config.volatility_period :]
        if all(math.isfinite(value) for value in window):
            put("volatility", pstdev(window))
        else:
            values["volatility"] = Feature(None, "numeric_overflow")
    if count >= config.volume_period:
        put("volume_sma", math.fsum(volumes[-config.volume_period :]) / config.volume_period)
    if count > config.volume_period:
        baseline = math.fsum(volumes[-config.volume_period - 1 : -1]) / config.volume_period
        if baseline == 0:
            values["relative_volume"] = Feature(None, "zero_volume_baseline")
        else:
            put("relative_volume", volumes[-1] / baseline)
    return FeatureVector(symbol, session_date, as_of, config, values)
