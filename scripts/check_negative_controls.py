"""Confirm each quality gate rejects a known defect; run in the locked environment."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


def main() -> int:
    examples = [
        ("lint.py", "import os\n", ["ruff", "check", "--isolated", "--select", "F401"]),
        ("invalid_types.py", 'value: int = "wrong"\n', ["mypy", "--strict"]),
        ("test_failure.py", "def test_failure():\n    assert False\n", ["pytest", "-q"]),
    ]
    with TemporaryDirectory(prefix="prediction-bot-negative-") as directory:
        for filename, source, command in examples:
            path = Path(directory) / filename
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", *command, str(path)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
            # These tools return 1 for a finding/test failure, rather than a crash.
            if result.returncode != 1:
                print(f"Unexpected {command[0]} exit status: {result.returncode}")
                print(result.stdout, result.stderr)
                return 1
            print(f"{command[0]} rejected its deliberate defect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
