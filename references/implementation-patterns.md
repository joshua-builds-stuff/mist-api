# Mist implementation patterns

Read this file for Python, cURL, pagination, retries, rate limits, or production automation.

## Reuse the tested client

Use `examples/mist_client.py` as the reference implementation for generated Python scripts. Preserve these properties:

- Validate HTTPS and the destination hostname before constructing an authorization header.
- Keep tokens in environment variables; never include them in exceptions or logs.
- Reuse a `requests.Session` for connection pooling.
- Set a finite connect/read timeout.
- Retry connection failures, HTTP 429, and transient 5xx responses only for safe reads by default.
- Bound exponential backoff, add jitter, and cap `Retry-After` values.
- Do not automatically retry POST, PUT, PATCH, or DELETE unless current API evidence proves replay safety.
- Raise errors containing status, not response bodies or secret-bearing payloads.

## JSON request bodies

`MistClient.request` and `request_json` take `json_body` as `None`, a mapping, or a non-string sequence:

- `None` sends no JSON body.
- A mapping is shallow-copied with `dict()` and sent as a JSON object; later caller changes do not affect the copy. Object bodies, including the WLAN stub's `{"ssid": ...}` PUT, keep this behavior.
- A list, tuple, or other non-`str`/`bytes`/`bytearray` sequence is shallow-copied with `list()` and sent as a JSON array, including a list of two-character strings. Arrays of strings or objects are both forwarded unchanged.
- Any other value — string, bytes, set, number — raises `ValueError` (`Mist API json_body must be a JSON object or array`) before any request is sent.

When `show` labels the request body `array` or `array[...]`, pass a list. Confirm the path and item schema with `show` or `operation` first. Example for a body that is an array of strings (org inventory claim):

```python
client.request_json(
    "POST",
    "/orgs/ORG_ID/inventory",
    json_body=["CLAIM_CODE", "CLAIM_CODE"],
)
```

Pagination, retries, authentication, and host checks are unchanged by body shape.

## Pagination

Assume list operations are paginated unless the current operation proves otherwise. Verify the operation's pagination parameters and response shape, request an explicit page size, continue until a short or empty page, bound maximum pages or records for unattended automation, and deduplicate stable identifiers if the endpoint can change during traversal. Do not describe a result as "all" when only one page was requested.

`MistClient.paginate` applies those bounds for `page`/`limit` list endpoints:

- `page_size` 1–100 (default 100); `max_pages` 1–10000 (default 1000). Caller-supplied `page`/`limit` params are ignored.
- A page shorter than `page_size` ends the walk.
- A full page at `max_pages` triggers one probe of page `max_pages + 1` using the normal GET retry rules. An empty probe completes the result; a non-empty probe raises `MistAPIError` (`pagination exceeded the N-page safety limit`) after the allowed pages' records were already yielded.
- A caller that replaces its output file only after iteration finishes, such as `examples/get_site_devices_to_csv.py`, therefore writes the export when the last allowed page is exactly full and leaves the destination unchanged when the probe has records.

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
