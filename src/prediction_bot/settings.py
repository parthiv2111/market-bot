"""Explicit configuration loading; secrets never enter policy snapshots or errors."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

Percentage = Annotated[float, Field(gt=0, le=100, allow_inf_nan=False, strict=True)]


class RiskPolicy(BaseModel):
    """Required research limits in percentage points, not fractional returns."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    per_trade_risk_pct: Percentage
    portfolio_heat_pct: Percentage
    daily_loss_limit_pct: Percentage
    weekly_loss_limit_pct: Percentage
    monthly_loss_limit_pct: Percentage
    drawdown_stop_pct: Percentage
    profit_protect_pct: Percentage
    exceptions_enabled: Literal[False] = False

    @model_validator(mode="after")
    def coherent_limits(self) -> "RiskPolicy":
        if not self.per_trade_risk_pct <= self.portfolio_heat_pct <= self.daily_loss_limit_pct:
            raise ValueError("Require per-trade risk <= heat <= daily loss limit")
        if (
            not self.daily_loss_limit_pct
            <= self.weekly_loss_limit_pct
            <= self.monthly_loss_limit_pct
        ):
            raise ValueError("Require daily <= weekly <= monthly loss limits")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    mode: Literal["research", "paper", "shadow"] = "paper"
    provider: Literal["local_csv", "nse_eod", "yfinance"] = "local_csv"
    strategy: Literal["daily_five_session"] = "daily_five_session"
    risk: RiskPolicy
    provider_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    database_url: SecretStr | None = Field(default=None, exclude=True, repr=False)


class ConfigurationError(ValueError):
    """Sanitized boundary error: callers can log it without revealing input."""


def load_settings(path: Path) -> Settings:
    try:
        return Settings.model_validate_json(path.read_bytes())
    except (OSError, ValidationError, ValueError) as error:
        # Do not chain raw parser errors: their input may contain secrets.
        if isinstance(error, ValidationError):
            fields = sorted({".".join(map(str, item["loc"])) for item in error.errors()})
            message = "Invalid configuration fields: " + ", ".join(fields)
            # Arbitrary extra-field names themselves may contain sensitive input.
            if any(item["type"] == "extra_forbidden" for item in error.errors()):
                message = "Invalid configuration: unexpected fields"
        else:
            message = "Configuration file is unreadable or invalid"
        raise ConfigurationError(message) from None


def policy_payload(settings: Settings) -> bytes:
    """Only explicitly public settings participate in the policy identity."""
    return json.dumps(
        settings.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
