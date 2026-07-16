# Mist Terraform guidance

Read this file for Terraform resources, imports, state, drift, and declarative rollout.

- Prefer Terraform when desired configuration should be durable, reviewable, and reconciled over time.
- Prefer a script for one-time migration, reporting, operational queries, or workflows not represented by the provider.
- Verify resource names and arguments in current official Mist Terraform provider documentation; the REST OpenAPI export does not prove Terraform coverage.
- Map the Mist object, Terraform resource, key arguments, ownership boundary, and verification read.
- Import existing objects before managing them when supported.
- Review plans for destructive replacements, omitted defaults, and secrets stored in state or plan output.
- Use an encrypted remote state backend, access controls, locking, version pinning, and recovery procedures for production.
- Roll out to one low-impact site or object before broader application.
- Do not let Terraform and imperative scripts manage the same fields; define ownership to prevent drift loops.
- Explain rollback implications: reverting configuration does not necessarily reverse provider-side replacement or recover deleted objects.
