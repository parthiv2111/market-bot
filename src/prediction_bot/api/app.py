"""Read-only health stub; no startup connections or background execution."""

from fastapi import FastAPI

from prediction_bot.health import HealthStatus, health_status


def create_app() -> FastAPI:
    app = FastAPI(title="Prediction Bot", version="0.1.0")

    @app.get("/health")
    def health() -> HealthStatus:
        return health_status()

    return app
