#!/usr/bin/env python3
"""Scan tracked files and reachable Git history without printing secret values.

Requires development dependencies. Verification is disabled: discovered values
are never sent to a provider. Only one exact, reviewed invalid-URL fixture is
excluded, including historical copies; entire test files are never allowlisted.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from detect_secrets.core.scan import scan_file
from detect_secrets.settings import default_settings


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], timeout=60)  # noqa: S603


def known_fixture(path: str, line: str, detector: str) -> bool:
    dummy = '"https://' + "user" + ":" + "password" + '@api.mist.com/api/v1",'
    return (
        path == "tests/test_examples.py"
        and detector == "Basic Auth Credentials"
        and line.strip() == dummy
    )


def scan_content(path: str, content: bytes, scratch: Path) -> list[str]:
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    scratch.write_bytes(content)
    return [
        f"{path}:{secret.line_number}: {secret.type}"
        for secret in scan_file(str(scratch))
        if not known_fixture(path, lines[secret.line_number - 1], secret.type)
    ]


def main() -> int:
    findings: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="mist-secret-scan-") as directory:
        scratch = Path(directory) / "source.txt"
        with default_settings() as settings:
            settings.disable_filters(
                "detect_secrets.filters.common.is_ignored_due_to_verification"
            )
            for name in git("ls-files", "-z").decode().split("\0"):
                if name and Path(name).is_file():
                    findings.update(
                        scan_content(name, Path(name).read_bytes(), scratch)
                    )
            for entry in git("rev-list", "--objects", "--all").decode().splitlines():
                oid, separator, name = entry.partition(" ")
                if not separator or git("cat-file", "-t", oid).strip() != b"blob":
                    continue
                findings.update(
                    scan_content(name, git("cat-file", "blob", oid), scratch)
                )
    if findings:
        print("Potential secrets found (values omitted):")
        print("\n".join(sorted(findings)))
        return 1
    print("Tracked files and reachable Git history: no unexpected secret candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
