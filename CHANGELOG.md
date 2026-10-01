# Revision history

## Unreleased — public-release hardening

- Bound rollback-file reads even when a file grows after the size check, and
  report JSON parser-limit failures without a traceback or private content.
- Treat HTTP 5xx/408, redirects and transport failures as uncertain WLAN write
  outcomes. Verify without replaying the PUT; if verification also fails, save
  a separate private pending recovery record without replacing confirmed rollback.
  HTTP error classification now uses structured status fields, not message text.
- Bound webhook connections, inactivity and total lifetime before HTTP parsing;
  reject duplicate framing/signature headers and unsupported Transfer-Encoding.
  Normalize parser-limit failures into generic errors and validate env defaults.
- Bound and memoize schema lookup/ref traversal, cap structural nesting, honor
  required-only allOf/ref sibling constraints, bound actual spec reads, and retain
  only the best limited endpoint search results.
- Raise the Requests minimum to 2.33. Add dependency and tracked/history secret
  scans, SHA-pin CI actions, enable update automation, and add macOS/Windows tests.
- Add safe tracked-file sharing instructions, credential/export ignore rules,
  environment setup, contribution guidance and private security-reporting guidance.
- Add regression tests for ambiguous writes, pending recovery, schema limits and
  constraints, and real HTTP handler admission/framing/deadline behavior.

## 2026-10-01 — **minor**

Documentation sync for engineering changes #18 and #19, already merged to `main`. No install, upgrade, environment, or prerequisite changes. No security-facing behavior change.

- `scripts/query_spec.py show` labels a parameter that is a `$ref`, including a single `$ref` inside `allOf`, `anyOf`, or `oneOf`. The label is the component name, its `type`, and `enum[...]` or `const=` when the component defines them. A required path parameter such as `band` prints `dot11_band string enum[24, 5, 5-dedicated, 5-selectable, 6, 6-dedicated, 6-selectable]` instead of `?`.
- Several component refs on one parameter are listed as `allOf[...]`, `anyOf[...]`, or `oneOf[...]`, joined with `; `, at most five labels, then `; +N more`. A parameter with a plain `type` is still that type. Request-body and response schema labels are unchanged.
- `MistClient.request` and `request_json` accept a JSON object or a JSON array as `json_body`. A mapping is still shallow-copied. An array is shallow-copied and sent unchanged, including a list of two-character strings. A string, bytes value, set, or number raises `ValueError` (`Mist API json_body must be a JSON object or array`) before any request is sent.
- Object bodies, including the WLAN stub PUT, are unchanged. Pagination, retries, authentication, and host checks are unchanged.

## 2026-10-01 — **minor**

Documentation sync for engineering change #16, already merged to `main`. No install, upgrade, environment, or prerequisite changes. No security-facing behavior change.

- `scripts/query_spec.py schema NAME --property FIELD` merges `allOf` constraints into one answer. `required` is true when any branch requires the field. `type` and `enum` keep the shared values. Numeric bounds keep the tighter limit. A constraint that cannot be combined stays on the field schema under `allOf`.
- A `oneOf` or `anyOf` field that differs by variant is reported per variant. The label is the discriminator mapping key, otherwise the `$ref` name, otherwise `oneOf[index]` or `anyOf[index]`. `variants` holds each variant's `required` and `schema`. `absentFrom` lists variants that omit the field. Top-level `required` and `schema` are omitted for that result.
- When every `oneOf` or `anyOf` variant defines the field with the same schema and `required` flag, the command keeps the flat `required` and `schema` shape. A composed model such as `deviceprofile` uses that flat shape for a shared field and the per-variant shape when the branches disagree.
- The letter-case ambiguity error is unchanged. `show` output is unchanged.

## 2026-09-28 — **minor**

Documentation sync for engineering changes already merged to `main`. No install, upgrade, environment, or prerequisite changes.

- `scripts/query_spec.py show` names component schemas nested in `allOf`, `anyOf`, `oneOf`, and array `items`.
- `schema` and `operation` JSON that exceeds `--max-chars` is shortened in place. Long text goes first, then nested schema labels, then trailing fields counted as `x-query-omitted`. The root schema remains until those reductions cannot fit.
- `MistClient.paginate` requests one page past `max_pages` when the last allowed page is full. An empty extra page completes the result. A non-empty extra page still raises the page safety limit.
- `examples/update_wlan_stub.py` replaces its rollback file after the new SSID is confirmed, and also when the PUT succeeded but the verification read cannot be completed. A rejected PUT, or a follow-up read that still shows the previous SSID, leaves the existing rollback file unchanged.
- Maintainer `ruff check` follows the rule set pinned in `ruff.toml` (`E4`, `E7`, `E9`, `F`, `B`).
