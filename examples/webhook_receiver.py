#!/usr/bin/env python3
"""Dependency-free local Juniper Mist webhook receiver (Python 3.10+).

The receiver binds to localhost by default, requires a shared secret, accepts
JSON only, and never logs request bodies or signature values.  For production,
place a hardened asynchronous service behind TLS and appropriate network policy.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
from collections.abc import Mapping
from hashlib import sha1, sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO

DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MAX_CONFIGURABLE_BODY_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CONNECTIONS = 32
MAX_CONFIGURABLE_CONNECTIONS = 128
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_CONNECTION_DEADLINE_SECONDS = 30.0
MAX_CONFIGURABLE_TIMEOUT_SECONDS = 300.0
SHA1_DIGEST_BYTES = 20
SHA256_DIGEST_BYTES = 32

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("mist-webhook-receiver")


class SignatureError(ValueError):
    """The request did not contain a valid Mist webhook signature."""


class WebhookRequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    result = None
    for key, value in headers.items():
        if key.casefold() == wanted:
            if result is not None:
                raise WebhookRequestError(
                    400, f"Duplicate {name} headers are not allowed"
                )
            result = value
    return result


def _signature_bytes(value: str, algorithm: str, digest_size: int) -> bytes:
    candidate = value.strip()
    prefix = f"{algorithm}="
    if candidate.casefold().startswith(prefix):
        candidate = candidate[len(prefix) :]
    if len(candidate) != digest_size * 2:
        raise SignatureError("Malformed Mist webhook signature")
    try:
        return bytes.fromhex(candidate)
    except ValueError:
        raise SignatureError("Malformed Mist webhook signature") from None


def verify_mist_signature(
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str | bytes,
) -> str:
    """Verify Mist v2 (SHA-256), falling back to v1 (SHA-1) only if absent."""

    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    if not secret_bytes:
        raise SignatureError("Webhook shared secret is not configured")

    provided_v2 = _header(headers, "X-Mist-Signature-v2")
    provided_v1 = _header(headers, "X-Mist-Signature")
    if provided_v2 is not None:
        try:
            supplied_v2 = _signature_bytes(
                provided_v2,
                "sha256",
                SHA256_DIGEST_BYTES,
            )
        except SignatureError:
            raise SignatureError("Invalid Mist v2 webhook signature") from None
        expected_v2 = hmac.new(secret_bytes, raw_body, sha256).digest()
        if not hmac.compare_digest(supplied_v2, expected_v2):
            # An invalid v2 signature must never be rescued by a valid v1 value.
            raise SignatureError("Invalid Mist v2 webhook signature")
        return "v2"

    if provided_v1 is None:
        raise SignatureError("Missing Mist webhook signature")
    try:
        supplied_v1 = _signature_bytes(provided_v1, "sha1", SHA1_DIGEST_BYTES)
    except SignatureError:
        raise SignatureError("Invalid Mist v1 webhook signature") from None
    expected_v1 = hmac.new(secret_bytes, raw_body, sha1).digest()
    if not hmac.compare_digest(supplied_v1, expected_v1):
        raise SignatureError("Invalid Mist v1 webhook signature")
    return "v1"


def read_request_body(
    headers: Mapping[str, str],
    stream: BinaryIO,
    *,
    max_body_bytes: int,
) -> bytes:
    """Validate request metadata and bound the read before allocation."""

    if _header(headers, "Transfer-Encoding") is not None:
        raise WebhookRequestError(400, "Transfer-Encoding is not supported")

    content_type = _header(headers, "Content-Type")
    media_type = (content_type or "").split(";", 1)[0].strip().casefold()
    if media_type != "application/json":
        raise WebhookRequestError(415, "Content-Type must be application/json")

    content_length = _header(headers, "Content-Length")
    if content_length is None:
        raise WebhookRequestError(411, "Content-Length is required")
    content_length = content_length.strip(" \t")
    if (
        not content_length
        or not content_length.isascii()
        or not content_length.isdecimal()
    ):
        raise WebhookRequestError(400, "Content-Length must contain only ASCII digits")
    normalized = content_length.lstrip("0") or "0"
    maximum = str(max_body_bytes)
    if len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    ):
        raise WebhookRequestError(413, "Webhook body exceeds the configured limit")
    length = int(normalized, 10)

    raw_body = stream.read(length)
    if len(raw_body) != length:
        raise WebhookRequestError(400, "Webhook body was shorter than Content-Length")
    return raw_body


def parse_json_body(raw_body: bytes) -> dict[str, Any] | list[Any]:
    """Decode a verified body as one JSON object or array."""

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, RecursionError):
        raise WebhookRequestError(
            400, "Webhook body must be valid UTF-8 JSON"
        ) from None
    if not isinstance(payload, (dict, list)):
        raise WebhookRequestError(400, "Webhook JSON must be an object or array")
    return payload


def authenticate_and_parse(
    headers: Mapping[str, str],
    stream: BinaryIO,
    *,
    secret: bytes,
    max_body_bytes: int,
) -> tuple[dict[str, Any] | list[Any], str, int]:
    """Read, authenticate, then parse in that security-sensitive order."""

    raw_body = read_request_body(
        headers,
        stream,
        max_body_bytes=max_body_bytes,
    )
    signature_version = verify_mist_signature(raw_body, headers, secret)
    return parse_json_body(raw_body), signature_version, len(raw_body)


def payload_event_count(payload: dict[str, Any] | list[Any]) -> int:
    if isinstance(payload, list):
        return len(payload)
    events = payload.get("events")
    return len(events) if isinstance(events, list) else 1


def validate_bind_host(host: str, *, allow_non_loopback: bool) -> str:
    """Keep the development server local unless exposure is explicit."""

    candidate = host.strip()
    if not candidate:
        raise ValueError("--host must not be empty")

    try:
        is_loopback = ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        is_loopback = candidate.casefold().rstrip(".") == "localhost"

    if not is_loopback and not allow_non_loopback:
        raise ValueError(
            "Non-loopback webhook binds require --allow-non-loopback; "
            "this development server does not provide TLS"
        )
    return candidate


class MistWebhookServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        secret: bytes,
        max_body_bytes: int,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        connection_deadline_seconds: float = DEFAULT_CONNECTION_DEADLINE_SECONDS,
    ) -> None:
        if (
            type(max_connections) is not int
            or not 1 <= max_connections <= MAX_CONFIGURABLE_CONNECTIONS
        ):
            raise ValueError(
                f"max_connections must be between 1 and {MAX_CONFIGURABLE_CONNECTIONS}"
            )
        for name, value in (
            ("request_timeout_seconds", request_timeout_seconds),
            ("connection_deadline_seconds", connection_deadline_seconds),
        ):
            if not 0 < value <= MAX_CONFIGURABLE_TIMEOUT_SECONDS:
                raise ValueError(
                    f"{name} must be greater than zero and at most {MAX_CONFIGURABLE_TIMEOUT_SECONDS:g}"
                )
        self.webhook_secret = secret
        self.max_body_bytes = max_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.connection_deadline_seconds = connection_deadline_seconds
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        try:
            if ipaddress.ip_address(server_address[0]).version == 6:
                self.address_family = socket.AF_INET6
        except ValueError:
            pass
        super().__init__(server_address, MistWebhookHandler)

    def process_request(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        if not self._connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(self.request_timeout_seconds)
            super().process_request(request, client_address)
        except BaseException:
            self._connection_slots.release()
            raise

    @staticmethod
    def _expire_connection(request: socket.socket) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # A completed worker may already have closed the socket.

    def process_request_thread(
        self, request: socket.socket, client_address: tuple[str, int]
    ) -> None:
        deadline = None
        try:
            deadline = threading.Timer(
                self.connection_deadline_seconds, self._expire_connection, (request,)
            )
            deadline.daemon = True
            deadline.start()
            self.finish_request(request, client_address)
        except (ConnectionError, TimeoutError):
            logger.info("Webhook connection closed before completion")
        except Exception:
            self.handle_error(request, client_address)
        finally:
            if deadline is not None:
                deadline.cancel()
            try:
                self.shutdown_request(request)
            finally:
                self._connection_slots.release()


class MistWebhookHandler(BaseHTTPRequestHandler):
    server: MistWebhookServer
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(dict(value), separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.close_connection = True
        if self.path != "/mist/webhook":
            self._send_json(404, {"ok": False, "error": "not found"})
            return

        try:
            payload, signature_version, body_length = authenticate_and_parse(
                self.headers,
                self.rfile,
                secret=self.server.webhook_secret,
                max_body_bytes=self.server.max_body_bytes,
            )
        except WebhookRequestError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
            return
        except SignatureError:
            self._send_json(401, {"ok": False, "error": "invalid signature"})
            return

        logger.info(
            "Accepted Mist webhook: bytes=%s payload_type=%s events=%s signature=%s",
            body_length,
            type(payload).__name__,
            payload_event_count(payload),
            signature_version,
        )
        self._send_json(200, {"ok": True})

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Deliberately omit the raw request line, which may contain attacker-
        # supplied query data.  Status and response size are sufficient here.
        logger.info("HTTP response: status=%s bytes=%s", code, size)

    def log_message(self, _format: str, *_args: object) -> None:
        # BaseHTTPRequestHandler uses this for raw client-controlled messages.
        logger.info("HTTP server event")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local Mist webhook receiver.")
    parser.add_argument(
        "--host", default="127.0.0.1", help="Listen address (default: localhost)"
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow a non-loopback bind (development only; no TLS)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("PORT", "8080"),
        help="Listen port",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=int,
        default=os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(DEFAULT_MAX_BODY_BYTES)),
        help="Maximum accepted request body size",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help="Maximum active connections (default: 32; maximum: 128)",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Socket inactivity timeout (default: 10 seconds)",
    )
    parser.add_argument(
        "--connection-deadline-seconds",
        type=float,
        default=DEFAULT_CONNECTION_DEADLINE_SECONDS,
        help="Total connection lifetime (default: 30 seconds)",
    )
    return parser.parse_args()


def _url_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    return f"[{host}]" if address.version == 6 else host


def main() -> int:
    args = parse_args()
    secret = os.environ.get("WEBHOOK_SHARED_SECRET")
    if not secret:
        raise SystemExit("Set WEBHOOK_SHARED_SECRET before starting the receiver.")
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 1 <= args.max_body_bytes <= MAX_CONFIGURABLE_BODY_BYTES:
        raise SystemExit(
            f"--max-body-bytes must be between 1 and {MAX_CONFIGURABLE_BODY_BYTES}"
        )

    try:
        host = validate_bind_host(
            args.host,
            allow_non_loopback=args.allow_non_loopback,
        )
        server = MistWebhookServer(
            (host, args.port),
            secret.encode("utf-8"),
            args.max_body_bytes,
            max_connections=args.max_connections,
            request_timeout_seconds=args.request_timeout_seconds,
            connection_deadline_seconds=args.connection_deadline_seconds,
        )
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None

    logger.info("Listening on http://%s:%s/mist/webhook", _url_host(host), args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping webhook receiver")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
