"""Versioned research regime rules; unknown inputs never authorize entries."""

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from prediction_bot.data_quality import _aware, _save
from prediction_bot.indicators import FeatureVector, IndicatorConfig, KnownBar, features_at
from prediction_bot.reference_data import KOLKATA, FrozenModel, ReferenceBundle

KnownRegime = Literal["bull", "bear", "range", "high_volatility"]
Regime = Literal["bull", "bear", "range", "high_volatility", "unknown"]


class RegimeRules(FrozenModel):
    implementation: Literal["regimes-v1"] = "regimes-v1"
    indicators: IndicatorConfig = IndicatorConfig(ema_period=10, sma_period=30)
    trend_threshold: float = Field(default=0.01, gt=0, lt=1, allow_inf_nan=False)
    high_volatility_threshold: float = Field(default=0.03, gt=0, allow_inf_nan=False)
    entry_regimes: tuple[KnownRegime, ...] = ()

    @model_validator(mode="after")
    def validate_rules(self) -> "RegimeRules":
        if self.indicators.ema_period >= self.indicators.sma_period:
            raise ValueError("Trend EMA must be shorter than trend SMA")
        if len(set(self.entry_regimes)) != len(self.entry_regimes):
            raise ValueError("Entry regimes must be distinct")
        return self

    @property
    def version(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


@dataclass(frozen=True)
class RegimeDecision:
    session_date: date
    as_of: datetime
    regime: Regime
    trend: Literal["bull", "bear", "range", "unknown"]
    volatility: Literal["normal", "high", "unknown"]
    entry_eligible: bool
    rule_version: str
    rule_payload: str
    inputs: dict[str, float | str | None]
    reasons: tuple[str, ...]

    def save(self, directory: Path) -> str:
        return _save(
            directory, json.dumps(asdict(self), sort_keys=True, default=str).encode(), "json"
        )


def classify_regime(
    index: FeatureVector | None,
    equity: FeatureVector | None,
    *,
    session_date: date,
    as_of: datetime,
    rules: RegimeRules | None = None,
) -> RegimeDecision:
    _aware(as_of)
    rules = rules or RegimeRules()
    inputs: dict[str, float | str | None] = {}
    reasons = []
    if session_date > as_of.astimezone(KOLKATA).date():
        reasons.append("future_session")
    if index is not None and equity is not None and index.symbol == equity.symbol:
        reasons.append("index_equity_identity_collision")
    measurements: dict[str, dict[str, float]] = {}
    for role, vector in (("index", index), ("equity", equity)):
        if vector is None:
            reasons.append(f"{role}:missing_features")
            continue
        inputs[f"{role}.symbol"] = vector.symbol
        inputs[f"{role}.feature_version"] = vector.version
        inputs[f"{role}.session_date"] = vector.session_date.isoformat()
        inputs[f"{role}.as_of"] = vector.as_of.isoformat()
        if (
            vector.session_date != session_date
            or vector.as_of != as_of
            or vector.config != rules.indicators
            or vector.price_basis != "raw"
            or vector.version != "indicators-v1"
        ):
            reasons.append(f"{role}:incompatible_features")
        values = {}
        for name in ("ema", "sma", "volatility"):
            feature = vector.values.get(name)
            value = None if feature is None else feature.value
            inputs[f"{role}.{name}"] = value
            if feature is None or feature.invalid_reason is not None or value is None:
                reason = (
                    "missing_feature"
                    if feature is None
                    else feature.invalid_reason or "missing_value"
                )
                reasons.append(f"{role}.{name}:{reason}")
            elif (
                not math.isfinite(value)
                or value > 1e100
                or (value < 1e-100 if name != "volatility" else value < 0)
            ):
                reasons.append(f"{role}.{name}:invalid_value")
            else:
                values[name] = value
        measurements[role] = values
    if reasons:
        return RegimeDecision(
            session_date,
            as_of,
            "unknown",
            "unknown",
            "unknown",
            False,
            rules.version,
            rules.model_dump_json(),
            inputs,
            tuple(reasons),
        )
    spreads = {role: values["ema"] / values["sma"] - 1 for role, values in measurements.items()}
    for role, spread in spreads.items():
        inputs[f"{role}.trend_spread"] = spread
    trend: Literal["bull", "bear", "range", "unknown"] = "range"
    if all(value >= rules.trend_threshold for value in spreads.values()):
        trend = "bull"
    elif all(value <= -rules.trend_threshold for value in spreads.values()):
        trend = "bear"
    high = any(
        values["volatility"] >= rules.high_volatility_threshold for values in measurements.values()
    )
    regime: Regime = "high_volatility" if high else trend
    return RegimeDecision(
        session_date,
        as_of,
        regime,
        trend,
        "high" if high else "normal",
        regime in rules.entry_regimes,
        rules.version,
        rules.model_dump_json(),
        inputs,
        (),
    )


def regime_at(
    observations: tuple[KnownBar, ...],
    reference: ReferenceBundle,
    *,
    index_symbol: str,
    equity_symbol: str,
    session_date: date,
    as_of: datetime,
    rules: RegimeRules | None = None,
) -> RegimeDecision:
    if index_symbol == equity_symbol:
        raise ValueError("Index and equity symbols must be distinct")
    rules = rules or RegimeRules()
    vectors = [
        features_at(
            observations,
            reference,
            symbol=symbol,
            session_date=session_date,
            as_of=as_of,
            config=rules.indicators,
        )
        for symbol in (index_symbol, equity_symbol)
    ]
    return classify_regime(
        vectors[0], vectors[1], session_date=session_date, as_of=as_of, rules=rules
    )
