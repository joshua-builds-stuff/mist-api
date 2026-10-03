from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"
sys.path.insert(0, str(EXAMPLES_DIR))

import webhook_receiver as webhook  # noqa: E402


def _require_ipv6_loopback() -> None:
    if not socket.has_ipv6:
        pytest.skip("IPv6 is not supported on this host")
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError as exc:
        pytest.skip(f"IPv6 loopback is unavailable: {exc}")


def test_server_binds_to_ipv6_loopback() -> None:
    _require_ipv6_loopback()
    host = webhook.validate_bind_host("::1", allow_non_loopback=False)

    server = webhook.MistWebhookServer((host, 0), b"secret", 1024)
    try:
        assert server.address_family == socket.AF_INET6
        assert server.server_address[0] == "::1"
    finally:
        server.server_close()


def test_main_exits_cleanly_when_bind_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_server(*_args: object, **_kwargs: object) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(webhook, "MistWebhookServer", failing_server)
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "secret")
    monkeypatch.setattr(sys, "argv", ["webhook_receiver.py", "--port", "8080"])

    with pytest.raises(SystemExit) as excinfo:
        webhook.main()

    assert excinfo.value.code == "bind failed"
