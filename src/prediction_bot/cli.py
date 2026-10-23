"""CLI entry points for the research application."""

import argparse
import json
from collections.abc import Sequence

from prediction_bot.health import health_status


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prediction Bot research tools")
    parser.add_argument("command", choices=["health"])
    parser.parse_args(argv)
    print(json.dumps(health_status(), sort_keys=True))
    return 0
