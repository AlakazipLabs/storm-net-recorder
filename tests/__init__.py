"""Test package.

Network tripwire. These tests mock every transport, so none of them has any
business opening a socket. When a mock is written wrongly the test does not
fail — it quietly succeeds against a live host, which is how a "no network"
test suite ends up making real HTTPS requests without anyone noticing.

Blocking connect() at the socket layer turns that silent success into a loud
failure naming the test that did it. Nothing here affects the shipped code.
"""

import socket


class NetworkAccessInTests(RuntimeError):
    """A test tried to reach the network. Mock the transport instead."""


def _blocked(*_args, **_kwargs):
    raise NetworkAccessInTests(
        "this test tried to open a network connection. The suite is fully "
        "mocked by design — patch the transport (urllib.request.urlopen, "
        "subprocess.run) rather than letting it reach a real host."
    )


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
