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

Publish or replace a saved rollback record after a read-back shows the intended change, and also when the write succeeded but the verification read cannot be completed. Leave the previous record in place when a rejected write did not apply. Keep the previous record when an uncertain failure is followed by a read that still shows the old value.

## WLAN SSID example

`examples/update_wlan_stub.py` stays read-only unless `--apply` and `--confirm-target SITE_ID/WLAN_ID` are both present. The PUT body is only `{"ssid": "<proposed SSID>"}`. That PUT is a single attempt.

The rollback file is `--rollback-file` (default `wlan_rollback.json`). The file is written after the PUT, and only when the apply changes the SSID. A dry run leaves it untouched. Before the PUT, the path must not be an existing directory and its parent directory must already exist.

The record contains `version`, `base_url`, `site_id`, `wlan_id`, `before_ssid`, `applied_ssid`, and `created_at`. It omits the rest of the WLAN object. It is stored with owner-only permissions (`0600`) by writing a temporary file in the same directory and replacing the destination.

The new record replaces the previous file in two cases:

- The follow-up GET returns the desired SSID. The command then prints `Update verified.`
- The PUT succeeded, and the follow-up GET could not be completed. The command still exits with an error: `SSID was updated but verification could not be completed`. The preview above that error shows the previous SSID.

If the SSID changed and the record cannot be saved, the reported error is `SSID was updated but the rollback record could not be saved`. That same preview shows the previous SSID. This applies both after a confirmed change and after a successful PUT whose verification read failed.

A PUT error whose text contains `failed with HTTP` is a completed rejection for any non-success status, including 4xx and 5xx. The script does not GET the WLAN again, and the previous file stays as it was.

Any other PUT error may still have been applied. That set is a timeout, a connection error, a chunked-encoding failure, any other request exception, and a 2xx body that was not valid JSON. The script then GETs the WLAN. When that GET shows the desired SSID, the command saves the new record and finishes as a verified update (`Update verified.`). When the SSID is unchanged, the previous file stays and the original PUT error is reported. When the PUT itself succeeded and the follow-up GET returns a different SSID, the previous file stays and verification fails.

`--rollback FILE` restores `before_ssid` only when the live SSID still equals the record's `applied_ssid`, the site and WLAN ids match, and the selected base URL matches the record. That mode does not write a new rollback file.

## Sensitive material

Treat WLAN, RADIUS, NAC, SNMP, webhook, and device configuration as potentially secret-bearing.

- Never print full configuration objects by default.
- Store rollback data with user-only permissions and only the fields required to reverse the change.
- Do not include response bodies in generic exceptions.
- Ask users to redact tokens, cookies, PSKs, shared secrets, personal identifiers, and unrelated tenant data.

## Troubleshooting order

Check these causes systematically:

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
