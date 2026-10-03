# Revision history

## 1.0.0 — 2026-10-03

First tagged public release. `VERSION` is 1.0.0, and installations updated
from this point are notified after a successful explicit refresh when a newer
version is published. The sections below record the pre-release revision
history.

## 2026-10-03 — minor

Documentation verification run, project versioning, and an optional update
notice. No change to API behavior, authentication, or cached-spec handling.

- Verify the documented install flow (tracked-source archive extraction), the
  maintainer checks, the offline cache check's no-cache exit, and every script
  and example entry point inside a disposable sandbox clone: 13 commands, all
  passing, with network access limited to loopback.
- Add a TL;DR, a Mermaid workflow diagram, a table of contents, and a
  documentation revision stamp to the README.
- Remove an authorship attribution line from AUDIT.md per documentation
  hygiene policy, and ignore `.wtfm/` documentation-revision state.
- Add a root `VERSION` file (1.0.0). After a successful explicit
  `refresh_openapi.py` download, the tool makes one bounded HTTPS request
  (pinned host, no redirects, 5-second timeout, 64-byte read) for the
  published `VERSION` and prints a one-line `NOTE:` when a newer skill
  version exists. Only validated `x.y.z` values are compared or printed,
  `--offline` still performs no network access, every failure is silent,
  and `MIST_SKILL_UPDATE_CHECK=0` disables the check.

## 2026-10-03 — minor

Token-efficiency pass over the assistant-loaded documentation. No install,
upgrade, environment, API, or security-facing behavior change.

- Compress `SKILL.md` and every file under `references/` while keeping all
  workflow, safety, evidence, and output-format rules; deep label and merge
  mechanics now live only in `references/openapi.md`.
- Simplify the densest README passages into plain language with pointers to
  the reference files, for readers who are not developers.
- Add explicit non-developer response guidance to `SKILL.md`: define Mist
  terms plainly, give copy-paste commands with placeholders, and state where
  each required value comes from.
- Record before/after context-size estimates in `AUDIT.md`.

## 2026-10-02 — publication presentation

- Make the README, skill heading, and assistant display name explicitly unofficial
  without changing the `mist-api` skill identifier or installation path.
- Include HPE in the non-affiliation disclaimer and clarify that the project MIT
  license does not license vendor documentation, logos, trademarks, or API services.
- Ignore common downloaded Mist specification filenames and reiterate that vendor
  exports must not be bundled with shared source or releases.

## 2026-10-01 — public-release hardening

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

Documentation sync for Dependabot CI and dependency pin updates already merged to `main` (#21, #22, #23, #24, #25, #27, and #28). Pull request #26 was closed as a duplicate of #23 and was not merged. These are install and CI pin floors only. No API changes. No security-facing behavior change.

- `.github/workflows/ci.yml` pins `actions/checkout` at v7.0.1 (`3d3c42e5aac5ba805825da76410c181273ba90b1`) and `actions/setup-python` at v7.0.0 (`5fda3b95a4ea91299a34e894583c3862153e4b97`). The test job and the platform-smoke job both use those SHA pins.
- `examples/requirements.txt` requires `requests>=2.34.2,<3`. The public-release hardening note above recorded a Requests minimum of 2.33.
- `requirements-dev.txt` requires `pytest>=9.1.1,<10`, `pip-audit>=2.10.1,<3`, `ruff>=0.16.9,<1`, and `bandit>=1.9.4,<2`.

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
