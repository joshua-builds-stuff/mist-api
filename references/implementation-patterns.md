# Mist implementation patterns

Read this file for Python, cURL, pagination, retries, rate limits, or production automation.

## Reuse the tested client

Use `examples/mist_client.py` as the reference implementation for generated Python scripts. Preserve these properties:

- Validate HTTPS and the destination hostname before constructing an authorization header.
- Keep tokens in environment variables and never include them in exceptions or logs.
- Reuse a `requests.Session` for connection pooling.
- Set a finite connect/read timeout.
- Retry connection failures, HTTP 429, and transient 5xx responses only for safe reads by default.
- Bound exponential backoff, add jitter, and cap `Retry-After` values.
- Do not automatically retry POST, PUT, PATCH, or DELETE unless current API evidence proves replay safety.
- Raise errors containing status, not response bodies or secret-bearing payloads.

## Pagination

Assume list operations are paginated unless the current operation proves otherwise.

1. Verify the operation's pagination parameters and response shape.
2. Request an explicit page size.
3. Continue until a short or empty page is returned.
4. Bound maximum pages or records for unattended automation.
5. Deduplicate stable identifiers if the endpoint can change during traversal.

Do not describe a result as “all” when only one page was requested.

## cURL

Validate `MIST_BASE_URL` before running a request, then use:

```bash
curl --fail --silent --show-error \
  "${MIST_BASE_URL:-https://api.mist.com/api/v1}/orgs/$ORG_ID/sites?limit=100&page=1" \
  -H "Authorization: Token $MIST_API_TOKEN" \
  -H "Accept: application/json"
```

Avoid verbose cURL modes when authorization headers are present.

## Efficiency and delivery

- Prefer organization-level or batched reads over per-device loops.
- Cache discovered IDs only for the current run unless persistence is requested.
- State required environment variables and Python dependencies.
- Include one validation command and expected output shape.
- Use atomic output writes and neutralize spreadsheet formulas in CSV exports.
- State any maximum-page, maximum-record, or retry bounds.
