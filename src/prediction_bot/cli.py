"""CLI entry points for the research application."""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from prediction_bot.health import health_status
from prediction_bot.settings import ConfigurationError, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prediction Bot research tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    validation = commands.add_parser("validate-config")
    validation.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate-config":
        try:
            load_settings(args.path)
        except ConfigurationError as error:
            print(str(error), file=sys.stderr)
            return 2
        print("Configuration is valid; live execution remains disabled")
    else:
        print(json.dumps(health_status(), sort_keys=True))
    return 0
