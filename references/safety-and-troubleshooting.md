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
