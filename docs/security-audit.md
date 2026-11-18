# Security audit data: mist-api

**TL;DR:** Automated checks found no real secrets and no known vulnerable dependencies. The one high finding is a deliberate fake URL in a test. It checks that the client rejects credentials in a URL.

> Documents **v1.0.1** (`d46dafd`) · Collected 2026-11-18

> This page lists what automated checks observed. It is not a penetration test or a guarantee. Secret values are never shown.

## Summary

- High: **1** (reviewed: test data, not a credential)
- Medium: **0**
- Low: **0**

## Checks performed

| Check | Result |
| --- | --- |
| Text files scanned for secret-like strings (static scan) | 42 files, 1 finding (below) |
| `python tools/check_secrets.py` (tracked files and reachable Git history) | `no unexpected secret candidates` |
| `bandit -q -r scripts examples` | No issues reported |
| `pip-audit -r requirements-dev.txt` | `No known vulnerabilities found` |
| Security policy (`SECURITY.md`) | Present |
| GitHub Actions workflows reviewed | 1 (`ci.yml`): `permissions: contents: read`, actions pinned to commit SHAs |
| Lockfiles | No package manifest. Dependencies are version ranges in `requirements-dev.txt` and `examples/requirements.txt` |

## Findings

| Severity | Check | Location | Note |
|---|---|---|---|
| high | `secret-string:url-credentials` | `tests/test_examples.py:105` | Fake `user:password@` URL. The test proves that `normalize_base_url` rejects a URL with credentials. `tools/check_secrets.py` exempts this exact line only. |

## Network access by the project

| Code | Destination | When |
| --- | --- | --- |
| `scripts/refresh_openapi.py` | `https://networkingdocs.hpe.com` only. Redirects to other hosts are blocked. | Only when you run it without `--offline`. |
| `scripts/refresh_openapi.py` update check | `https://raw.githubusercontent.com` (`VERSION`, 64 bytes, no redirects) | After a successful download. `MIST_SKILL_UPDATE_CHECK=0` turns it off. |
| `examples/mist_client.py` | Official Mist API hosts over HTTPS. Other hosts need `--allow-custom-base-url`. | Only when you run an example with a token. |
| `examples/webhook_receiver.py` | Listens on loopback. Other addresses need `--allow-non-loopback`. | Only when you run it. |

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md). Do not open a public issue for a vulnerability.
