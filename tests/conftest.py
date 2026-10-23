"""Reject external network access during the default test suite."""

import socket
from ipaddress import ip_address

import pytest


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    original_connect = socket.socket.connect

    def connect(connection, address):
        # Windows asyncio implements its local wakeup socketpair over loopback.
        if isinstance(address, tuple) and ip_address(address[0]).is_loopback:
            return original_connect(connection, address)
        raise AssertionError("Tests must use offline fixtures, not network connections")

    def denied(*args, **kwargs):
        raise AssertionError("Tests must use offline fixtures, not network connections")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", denied)
