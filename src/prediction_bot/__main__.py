"""Support python -m prediction_bot without running on import."""

from prediction_bot.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
