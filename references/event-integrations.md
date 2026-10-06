# Mist event integrations

Read this file for webhook and WebSocket designs.

## Choose the mechanism

- Use webhooks for event notifications and downstream workflows.
- Use WebSockets for continuous streams only when current official documentation supports the required data.
- Verify event types, subscription endpoints, authentication, and payload schemas against current official documentation or tenant output.

## Webhooks

For HTTP POST webhooks configured with a Mist secret, current official documentation defines:

- `X-Mist-Signature`: HMAC-SHA1 over the raw body.
- `X-Mist-Signature-v2`: HMAC-SHA256 over the raw body.

Prefer and verify the v2 header when present. If v2 is present but invalid, reject the request; never downgrade to v1. Use constant-time comparison.

Also:

- Enforce HTTPS in production, request-size limits, and JSON content type.
- Verify the signature over the exact raw bytes before parsing JSON.
- Return quickly and queue production processing asynchronously.
- Make handlers idempotent and tolerate duplicate or reordered events.
- Log topic, event count, and correlation identifiers instead of raw payloads.
- Do not expose a development server directly to the internet.

`examples/webhook_receiver.py` is a localhost test harness, not a production service:

- Refuses non-loopback binds unless `--allow-non-loopback` is passed explicitly; that flag adds no TLS or production hardening.
- Admits at most 32 connections (`--max-connections`, max 128), applies a 10-second socket inactivity timeout (`--request-timeout-seconds`) and a 30-second total connection deadline (`--connection-deadline-seconds`); timeouts must be positive and at most 300 seconds. These apply before parsing the request line or headers.
- Rejects duplicate framing/signature headers, unsupported Transfer-Encoding, and non-ASCII-decimal Content-Length. Connections close after each POST response.
- On a remote development machine, use `--host 0.0.0.0 --allow-non-loopback` only on a trusted network; never expose the harness directly to the internet.
- After a successful bind, logs `Listening on http://HOST:PORT/mist/webhook`. An IPv6 host is wrapped in brackets (`http://[::1]:8080/mist/webhook` for `--host ::1 --port 8080`). IPv4 addresses and hostnames are not wrapped (`http://127.0.0.1:8080/mist/webhook`, `http://localhost:8080/mist/webhook`). The logged host text is not rewritten. Bind checks and bind-failure exit are unchanged.

## WebSockets

- Implement reconnect with bounded exponential backoff and jitter.
- Handle documented heartbeat or keepalive behavior.
- Re-authenticate or resubscribe after reconnect when required.
- Detect stale connections and duplicate events.
- Apply backpressure or bounded queues so consumers cannot exhaust memory.

Mark designs as production-ready only when authentication, TLS, input limits, observability, graceful shutdown, and deployment hardening are addressed.
