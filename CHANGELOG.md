# Revision history

## 2026-09-28 — **minor**

Documentation sync for engineering changes already merged to `main`. No install, upgrade, environment, or prerequisite changes.

- `scripts/query_spec.py show` names component schemas nested in `allOf`, `anyOf`, `oneOf`, and array `items`.
- `schema` and `operation` JSON that exceeds `--max-chars` is shortened in place. Long text goes first, then nested schema labels, then trailing fields counted as `x-query-omitted`. The root schema remains until those reductions cannot fit.
- `MistClient.paginate` requests one page past `max_pages` when the last allowed page is full. An empty extra page completes the result. A non-empty extra page still raises the page safety limit.
- `examples/update_wlan_stub.py` replaces its rollback file after the new SSID is confirmed, and also when the PUT succeeded but the verification read cannot be completed. A rejected PUT, or a follow-up read that still shows the previous SSID, leaves the existing rollback file unchanged.
- Maintainer `ruff check` follows the rule set pinned in `ruff.toml` (`E4`, `E7`, `E9`, `F`, `B`).
