"""CLI entry points for the research application."""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import text

from prediction_bot.health import health_status
from prediction_bot.ingestion import IngestionPlan, job_identity, persist_batch, run_ingestion
from prediction_bot.persistence.database import database_engine
from prediction_bot.reference_data import ReferenceBundle
from prediction_bot.settings import ConfigurationError, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prediction Bot research tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    validation = commands.add_parser("validate-config")
    validation.add_argument("path", type=Path)
    ingestion = commands.add_parser("ingest")
    ingestion.add_argument("plan", type=Path)
    ingestion.add_argument("--reference", type=Path, required=True)
    ingestion.add_argument("--directory", type=Path, default=Path("data/ingestion"))
    args = parser.parse_args(argv)
    if args.command == "ingest":
        try:
            plan = IngestionPlan.model_validate_json(args.plan.read_bytes())
            reference = ReferenceBundle.model_validate_json(args.reference.read_bytes())
            engine = database_engine(os.environ["PREDICTION_BOT_DATABASE_URL"])
            try:
                with engine.connect() as connection:
                    lock = int(job_identity(plan, reference)[:15], 16)
                    acquired = connection.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": lock}
                    )
                    connection.commit()
                    if not acquired:
                        raise ValueError("Ingestion job is already running")
                    try:
                        report = run_ingestion(
                            plan,
                            reference,
                            args.directory,
                            lambda batch, run_id: persist_batch(
                                connection, batch, run_id, plan.provider
                            ),
                        )
                    finally:
                        connection.rollback()
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock})
                        connection.commit()
            finally:
                engine.dispose()
        except Exception as error:
            print(f"Ingestion failed: {type(error).__name__}", file=sys.stderr)
            return 2
        print(json.dumps(report, sort_keys=True))
        return 1 if report["status"] == "failed" else 0
    elif args.command == "validate-config":
        try:
            load_settings(args.path)
        except ConfigurationError as error:
            print(str(error), file=sys.stderr)
            return 2
        print("Configuration is valid; live execution remains disabled")
    else:
        print(json.dumps(health_status(), sort_keys=True))
    return 0
