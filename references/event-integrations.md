# Mist event integrations

Read this file for webhook and WebSocket designs.

## Choose the mechanism

- Use webhooks for event notifications and downstream workflows.
- Use WebSockets for continuous streams only when the current official documentation supports the required data.
- Verify event types, subscription endpoints, authentication, and payload schemas against current official documentation or tenant output.

## Webhooks

For HTTP POST webhooks configured with a Mist secret, current official documentation defines:

- `X-Mist-Signature`: HMAC-SHA1 over the raw body.
- `X-Mist-Signature-v2`: HMAC-SHA256 over the raw body.

Prefer and verify the v2 header when present. If v2 is present but invalid, reject the request and never downgrade to v1. Use constant-time comparison.

Also:

- Enforce HTTPS in production, request-size limits, and JSON content type.
- Verify the signature over the exact raw bytes before parsing JSON.
- Return quickly and queue production processing asynchronously.
- Make handlers idempotent and tolerate duplicate or reordered events.
- Log topic, event count, and correlation identifiers instead of raw payloads.
- Do not expose a development server directly to the internet.

Treat `examples/webhook_receiver.py` as a localhost test harness, not a production service. It refuses non-loopback bind addresses unless `--allow-non-loopback` is supplied explicitly; that flag does not add TLS or production hardening.

The harness admits at most 32 connections (`--max-connections`, maximum 128),
sets a 10-second socket inactivity timeout (`--request-timeout-seconds`), and
enforces a 30-second total connection deadline (`--connection-deadline-seconds`).
Timeouts must be positive and at most 300 seconds. These apply before parsing
the request line or headers, not only to authenticated body processing. It rejects
duplicate framing/signature headers, unsupported Transfer-Encoding, and non-ASCII
decimal Content-Length. Connections close after each POST response. For a remote
development machine, explicitly use `--host 0.0.0.0 --allow-non-loopback` only on
a trusted network; never expose this harness directly to the internet.

## WebSockets

- Implement reconnect with bounded exponential backoff and jitter.
- Handle documented heartbeat or keepalive behavior.
- Re-authenticate or resubscribe after reconnect when required.
- Detect stale connections and duplicate events.
- Apply backpressure or bounded queues so consumers cannot exhaust memory.

Mark designs as production-ready only when authentication, TLS, input limits, observability, graceful shutdown, and deployment hardening are addressed.
