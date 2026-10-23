"""Behavioral acceptance checks for the installed package."""

import json
import subprocess
import sys

from fastapi.testclient import TestClient

from prediction_bot.api.app import create_app


def test_cli_health_from_outside_repository(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "prediction_bot", "health"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "status": "ok",
        "version": "0.1.0",
        "mode": "research",
        "live_orders_enabled": False,
    }


def test_api_health_is_read_only():
    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["live_orders_enabled"] is False
        assert client.post("/health").status_code == 405
        assert client.post("/orders", json={"symbol": "TEST"}).status_code == 404


def test_imports_do_not_connect_or_start_processes(tmp_path):
    # A new interpreter ensures every module is imported for the first time.
    script = """
import importlib
import pkgutil
import socket
import subprocess
def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted external execution")
socket.socket.connect = forbidden
socket.create_connection = forbidden
subprocess.Popen.__init__ = forbidden
import prediction_bot
for module in pkgutil.walk_packages(prediction_bot.__path__, prediction_bot.__name__ + "."):
    importlib.import_module(module.name)
"""
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, check=True)


def test_unknown_command_cannot_enable_live_execution(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "prediction_bot", "live"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert not result.stdout
