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


class _FakeServer:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def serve_forever(self) -> None:
        return None

    def server_close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("host", "expected_url"),
    [
        ("::1", "http://[::1]:8080/mist/webhook"),
        ("127.0.0.1", "http://127.0.0.1:8080/mist/webhook"),
        ("localhost", "http://localhost:8080/mist/webhook"),
    ],
)
def test_main_logs_listen_url_with_bracketed_ipv6_host(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    host: str,
    expected_url: str,
) -> None:
    monkeypatch.setattr(webhook, "MistWebhookServer", _FakeServer)
    monkeypatch.setenv("WEBHOOK_SHARED_SECRET", "secret")
    monkeypatch.setattr(
        sys, "argv", ["webhook_receiver.py", "--host", host, "--port", "8080"]
    )

    with caplog.at_level("INFO", logger=webhook.logger.name):
        assert webhook.main() == 0

    assert f"Listening on {expected_url}" in caplog.messages
