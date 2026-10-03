# Mist safety and troubleshooting

Read this file for configuration changes, destructive or bulk operations, debugging, redaction, and failure analysis.

## Authorization boundary

Treat requests to explain, draft, review, or troubleshoot as non-executing. Perform a tenant mutation only when the user explicitly requests execution, the exact target is identified, and confirmation is obtained immediately before the write or delete.

For POST, PUT, PATCH, DELETE, template changes, or bulk changes:

1. Name the exact object and impact scope.
2. Read current state and capture only the fields needed for rollback.
3. Redact secrets and personal data from diffs, logs, and model context.
4. Verify whether the operation replaces or partially updates the object.
5. Build the smallest schema-valid body.
6. Show the intended field-level change.
7. Re-read immediately before applying and abort on concurrent change.
8. Require target confirmation at the mutation boundary.
9. Apply without automatic write retries unless replay safety is proven.
10. Re-read and assert the intended fields.
11. Provide and test a practical rollback path.
12. Test one low-impact object before bounded bulk rollout.

Never assume PATCH semantics or that omitted PUT fields are preserved. Never send an entire GET response back as an update without proving every included field is writable and replacement-safe.

Publish or replace a saved rollback record after a read-back shows the intended change, and also when the write succeeded but the verification read could not be completed. Leave the previous record in place when a rejected write did not apply, and when an uncertain failure is followed by a read that still shows the old value.

## WLAN SSID example

`examples/update_wlan_stub.py` stays read-only unless `--apply` and `--confirm-target SITE_ID/WLAN_ID` are both present. The PUT body is only `{"ssid": "<proposed SSID>"}`, sent exactly once.

Rollback file (`--rollback-file`, default `wlan_rollback.json`):

- Written after the PUT, and only when the apply changes the SSID; a dry run leaves it untouched. Before the PUT, the path must not be an existing directory and its parent must exist.
- Contains only `version`, `base_url`, `site_id`, `wlan_id`, `before_ssid`, `applied_ssid`, `created_at`. Stored with owner-only permissions (`0600`) via a same-directory temporary file and atomic replace.
- Replaced in two cases: the follow-up GET returns the desired SSID (`Update verified.`), or the PUT succeeded but the follow-up GET could not be completed (the command still exits with `SSID was updated but verification could not be completed`; the preview above shows the previous SSID).
- If the SSID changed but the record cannot be saved, the error is `SSID was updated but the rollback record could not be saved`, and the preview shows the previous SSID.

Error classification uses `MistAPIError.status_code`, never message text:

- A structured HTTP 4xx other than 408 is a rejection; the previous rollback file is retained.
- HTTP 5xx, 408, redirects, transport errors (timeout, connection error, chunked-encoding failure, other request exceptions), and a 2xx body that was not valid JSON are indeterminate: the server may have applied the change. The script GETs the WLAN again without retrying the PUT and assumes no specific Mist backend behavior.
  - GET shows the desired SSID → the new record is saved and the update finishes verified.
  - GET shows the SSID unchanged → the previous file stays; the original PUT error is reported.
  - PUT succeeded but the GET returns a different SSID → the previous file stays; verification fails.
- Indeterminate PUT followed by a failed verification GET → the command reports `Write outcome is unknown` and saves `<rollback-file>.pending.json` (owner-only) with the same minimal before/intended values and `outcome: indeterminate`; the confirmed rollback file stays unchanged. Do not blindly replay the write: inspect the live WLAN and reconcile the pending record first. Pending records are rejected by `--rollback` and retained for manual review.

`--rollback FILE` restores `before_ssid` only when the live SSID still equals the record's `applied_ssid`, the site and WLAN ids match, and the selected base URL matches the record. That mode does not write a new rollback file.

## Sensitive material

Treat WLAN, RADIUS, NAC, SNMP, webhook, and device configuration as potentially secret-bearing:

- Never print full configuration objects by default.
- Store rollback data with user-only permissions and only the fields required to reverse the change.
- Do not include response bodies in generic exceptions.
- Ask users to redact tokens, cookies, PSKs, shared secrets, personal identifiers, and unrelated tenant data.

## Troubleshooting order

Check causes systematically:

1. Wrong regional cloud or base URL.
2. Wrong org, site, device, template, or object ID.
3. Ambiguous name-to-ID resolution.
4. Wrong object scope or hierarchy.
5. Missing token permission or tenant feature entitlement.
6. Invalid JSON or missing required fields.
7. Deprecated endpoint or stale schema assumption.
8. Pagination mistakes.
9. Rate limiting or bounded-retry exhaustion.
10. Template or organization policy controlling the object.
11. Object renamed, moved, deleted, or changed concurrently.

Rank likely causes from evidence. Supply a minimal reproduction request, a validation GET, and a corrected implementation. Describe undocumented status behavior as a hypothesis, not a fact.

## ID discovery

When the user knows names but not IDs, list the containing scope, collect exact normalized-name matches, fail on duplicates, and retrieve the selected object before use. Prefer an explicit ID argument for unattended automation.
