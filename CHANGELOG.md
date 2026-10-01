# Revision history

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
