"""Process health only: does not claim database or execution readiness."""

from typing import Literal, TypedDict

from prediction_bot import __version__


class HealthStatus(TypedDict):
    status: Literal["ok"]
    version: str
    mode: Literal["research"]
    live_orders_enabled: Literal[False]


def health_status() -> HealthStatus:
    return {
        "status": "ok",
        "version": __version__,
        "mode": "research",
        "live_orders_enabled": False,
    }
