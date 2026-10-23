"""The suite must fail before attempting a provider connection."""

import socket

import pytest


def test_network_connections_are_rejected():
    with pytest.raises(AssertionError, match="offline fixtures"):
        socket.create_connection(("example.invalid", 443))


def test_raw_socket_connections_are_rejected():
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="offline fixtures"):
            connection.connect(("192.0.2.1", 443))
