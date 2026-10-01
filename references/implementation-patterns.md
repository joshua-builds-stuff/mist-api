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

## JSON request bodies

`MistClient.request` and `MistClient.request_json` take `json_body` as `None`, a mapping, or a non-string sequence.

- `None` sends no JSON body.
- A mapping is shallow-copied with `dict()` and sent as a JSON object. Later changes to the caller's mapping do not change that copy. Object bodies, including the WLAN stub's `{"ssid": ...}` PUT, keep this behavior.
- A list, tuple, or other sequence that is not `str`, `bytes`, or `bytearray` is shallow-copied with `list()` and sent as a JSON array. A list of two-character strings stays an array. Arrays of strings or objects are both forwarded this way.
- Any other value, including a string, bytes, a set, or a number, raises `ValueError` with the message `Mist API json_body must be a JSON object or array`. No HTTP request is sent.

When `show` labels the request body as `array` or `array[...]`, pass a list. Confirm the path and the item schema with `show` or `operation` before calling. An operation whose cached spec body is an array of strings, such as org inventory claim, is called like this:

```python
client.request_json(
    "POST",
    "/orgs/ORG_ID/inventory",
    json_body=["CLAIM_CODE", "CLAIM_CODE"],
)
```

Pagination, retries, authentication, and host checks are unchanged.

## Pagination

Assume list operations are paginated unless the current operation proves otherwise.

1. Verify the operation's pagination parameters and response shape.
2. Request an explicit page size.
3. Continue until a short or empty page is returned.
4. Bound maximum pages or records for unattended automation.
5. Deduplicate stable identifiers if the endpoint can change during traversal.

Do not describe a result as “all” when only one page was requested.

`MistClient.paginate` in `examples/mist_client.py` applies that bound for `page`/`limit` list endpoints. `page_size` must be from 1 to 100 (default 100). `max_pages` must be from 1 to 10000 (default 1000). Any caller-supplied `page` or `limit` is ignored; the client sends its own.

A page shorter than `page_size` ends the walk. A completely full page on page `max_pages` is complete when the following page is empty, so the client requests page `max_pages + 1` once. An empty extra page means every record has been yielded. A non-empty extra page raises `MistAPIError` (`pagination exceeded the N-page safety limit`) after the records from the allowed pages have already been yielded. A caller that replaces its output file only after iteration finishes, including `examples/get_site_devices_to_csv.py`, therefore writes the export when the last allowed page is exactly full. When the extra page still has records, the destination file is left unchanged. The extra page uses the same GET retry rules as any other read.

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
