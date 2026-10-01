# Contributing

Use Python 3.10+ in an isolated virtual environment. Install
`requirements-dev.txt`, then run:

```bash
python -m pytest -q
ruff check .
ruff format --check .
bandit -q -r scripts examples
pip-audit -r requirements-dev.txt
python tools/check_secrets.py
```

Keep documentation queries separate from tenant actions. Add regression tests for
bug fixes, preserve explicit mutation confirmation and secret-safe errors, and
use synthetic data/fake clients instead of live credentials. Network test servers
must bind to `0.0.0.0` with ephemeral ports and shut down in fixture cleanup.

Do not commit vendor exports, API tokens, tenant exports, Terraform state, or
rollback files. The secret scanner reviews tracked files and fetched reachable
Git history, reports locations only, and does not validate credentials online.
It exempts one exact reviewed invalid-URL test fixture, not entire test files.

Describe behavior/compatibility changes in CHANGELOG.md and keep SKILL.md,
README.md and relevant references consistent. Submit a focused pull request.
Maintainers should distribute reviewed tagged source archives (`git archive` or
GitHub source archives), not arbitrary working-folder copies. Publishing a tag or
enabling GitHub private reporting is a separate maintainer action.
