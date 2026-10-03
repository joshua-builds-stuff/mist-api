#!/usr/bin/env python3
"""Explicitly refresh or inspect the per-user Mist OpenAPI cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

try:
    from .spec_cache import (
        MAX_SPEC_BYTES,
        SpecValidationError,
        read_version,
        resolve_spec_path,
        validate_spec,
    )
except ImportError:  # Direct execution: ``python scripts/refresh_openapi.py``.
    from spec_cache import (  # type: ignore[no-redef]
        MAX_SPEC_BYTES,
        SpecValidationError,
        read_version,
        resolve_spec_path,
        validate_spec,
    )


SPEC_URL = (
    "https://www.juniper.net/documentation/us/en/software/mist/api/static/exports/"
    "mist-api-openapi31json.json"
)
_OFFICIAL_HOST = "www.juniper.net"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_REQUEST_TIMEOUT_SECONDS = 30
_USER_AGENT = "mist-api-skill/1.0"

SKILL_REPO_URL = "https://github.com/joshua-builds-stuff/mist-api"
_UPDATE_CHECK_URL = (
    "https://raw.githubusercontent.com/joshua-builds-stuff/mist-api/main/VERSION"
)
_UPDATE_CHECK_HOST = "raw.githubusercontent.com"
_UPDATE_CHECK_TIMEOUT_SECONDS = 5
_MAX_VERSION_BYTES = 64
_VERSION_PATTERN = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
_SKILL_ROOT = Path(__file__).resolve().parent.parent


class RefreshError(RuntimeError):
    """Raised when a refresh cannot safely produce a valid cache file."""


@dataclass(frozen=True)
class RefreshResult:
    version: str
    metadata_warning: str | None = None


def _make_file_private(file_descriptor: int, path: Path) -> None:
    """Apply owner-only POSIX modes with a Windows-compatible fallback."""

    mode = stat.S_IRUSR | stat.S_IWUSR
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(file_descriptor, mode)
    else:
        os.chmod(path, mode)


def _require_official_https_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise RefreshError("source returned an invalid redirect URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != _OFFICIAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise RefreshError(
            "redirect blocked: downloads must remain on https://www.juniper.net"
        )


class _OfficialHttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _require_official_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _content_type(headers) -> str:  # noqa: ANN001
    raw_value = headers.get("Content-Type", "") or ""
    return raw_value.partition(";")[0].strip().lower()


def _require_json_response(response) -> None:  # noqa: ANN001
    media_type = _content_type(response.headers)
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise RefreshError(
            f"server returned non-JSON content type {media_type or '(missing)'}"
        )

    raw_length = response.headers.get("Content-Length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise RefreshError("server returned an invalid Content-Length") from exc
    if content_length < 0:
        raise RefreshError("server returned an invalid Content-Length")
    if content_length > MAX_SPEC_BYTES:
        raise RefreshError(f"download exceeds the {MAX_SPEC_BYTES}-byte size limit")


def _prepare_cache_directory(directory: Path) -> None:
    existed = directory.exists()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        try:
            directory.chmod(stat.S_IRWXU)
        except OSError as exc:
            raise RefreshError(f"cannot make cache directory private: {exc}") from exc


def _stream_response(response, output) -> tuple[int, str]:  # noqa: ANN001
    total = 0
    digest = hashlib.sha256()
    while True:
        chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
        if not chunk:
            return total, digest.hexdigest()
        total += len(chunk)
        if total > MAX_SPEC_BYTES:
            raise RefreshError(f"download exceeds the {MAX_SPEC_BYTES}-byte size limit")
        digest.update(chunk)
        output.write(chunk)


def _metadata_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.stem}.metadata.json")


def _write_metadata_temp(
    cache_path: Path,
    *,
    version: str,
    byte_length: int,
    sha256: str,
) -> Path:
    metadata = {
        "source_url": SPEC_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha256": sha256,
        "byte_length": byte_length,
        "openapi_version": version,
    }
    metadata_path = _metadata_path(cache_path)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{metadata_path.name}-",
        suffix=".tmp",
        dir=cache_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        _make_file_private(file_descriptor, temporary_path)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            file_descriptor = -1
            json.dump(metadata, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        return temporary_path
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def refresh_spec(cache_path: Path, *, opener=None) -> RefreshResult:  # noqa: ANN001
    """Download, validate, and atomically replace ``cache_path``."""
    cache_path = resolve_spec_path(str(cache_path))
    _require_official_https_url(SPEC_URL)
    opener = opener or build_opener(_OfficialHttpsRedirectHandler())
    request = Request(SPEC_URL, headers={"User-Agent": _USER_AGENT})
    temporary_path: Path | None = None
    temporary_metadata_path: Path | None = None

    try:
        with opener.open(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            _require_official_https_url(response.geturl())
            _require_json_response(response)
            _prepare_cache_directory(cache_path.parent)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{cache_path.name}-",
                suffix=".tmp",
                dir=cache_path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                _make_file_private(file_descriptor, temporary_path)
                with os.fdopen(file_descriptor, "wb") as output:
                    file_descriptor = -1
                    byte_length, sha256 = _stream_response(response, output)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)

        version = validate_spec(temporary_path)
        temporary_metadata_path = _write_metadata_temp(
            cache_path,
            version=version,
            byte_length=byte_length,
            sha256=sha256,
        )
        os.replace(temporary_path, cache_path)
        temporary_path = None
        metadata_path = _metadata_path(cache_path)
        try:
            os.replace(temporary_metadata_path, metadata_path)
            temporary_metadata_path = None
        except OSError as exc:
            try:
                metadata_path.unlink(missing_ok=True)
                stale_note = "stale metadata was removed"
            except OSError:
                stale_note = "existing metadata may be stale"
            return RefreshResult(
                version,
                f"metadata update failed ({_one_line(exc)}); {stale_note}",
            )
        return RefreshResult(version)
    except RefreshError:
        raise
    except SpecValidationError as exc:
        raise RefreshError(f"downloaded OpenAPI document is invalid: {exc}") from exc
    except OSError as exc:
        raise RefreshError(f"cache update failed: {exc}") from exc
    except Exception as exc:
        raise RefreshError(f"download failed: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if temporary_metadata_path is not None:
            temporary_metadata_path.unlink(missing_ok=True)


def _one_line(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return message or error.__class__.__name__


class _RefuseRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        # Returning None makes urllib raise; the update check then stays silent.
        return None


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_PATTERN.fullmatch(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch)


def _format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def installed_skill_version() -> tuple[int, int, int] | None:
    try:
        raw = (_SKILL_ROOT / "VERSION").read_text(encoding="ascii")
    except (OSError, UnicodeError, ValueError):
        return None
    return _parse_version(raw)


def _update_check_enabled() -> bool:
    disabled = {"0", "false", "no", "off"}
    return os.environ.get("MIST_SKILL_UPDATE_CHECK", "").strip().lower() not in disabled


def check_for_skill_update(*, opener=None) -> str | None:  # noqa: ANN001
    """Return a one-line notice when a newer skill VERSION is published.

    Best effort: one bounded HTTPS request to the pinned project host, no
    redirects, no retries, and silence on every failure. Only strictly
    validated ``major.minor.patch`` values are compared or printed, so remote
    content can never inject text into the notice.
    """
    installed = installed_skill_version()
    if installed is None:
        return None
    opener = opener or build_opener(_RefuseRedirectHandler())
    request = Request(_UPDATE_CHECK_URL, headers={"User-Agent": _USER_AGENT})
    try:
        with opener.open(request, timeout=_UPDATE_CHECK_TIMEOUT_SECONDS) as response:
            parsed = urlsplit(response.geturl())
            if (
                parsed.scheme.lower() != "https"
                or (parsed.hostname or "").lower() != _UPDATE_CHECK_HOST
            ):
                return None
            raw = response.read(_MAX_VERSION_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw, bytes) or len(raw) > _MAX_VERSION_BYTES:
        return None
    try:
        published = _parse_version(raw.decode("ascii"))
    except UnicodeDecodeError:
        return None
    if published is None or published <= installed:
        return None
    return (
        f"NOTE: mist-api skill {_format_version(published)} is published; this "
        f"installation is {_format_version(installed)}. Update from "
        f"{SKILL_REPO_URL} and reinstall per the README."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="read the cache without network access or filesystem writes",
    )
    args = parser.parse_args(argv)

    try:
        cache_path = resolve_spec_path()
    except (OSError, ValueError) as exc:
        print(f"ERROR: Mist OpenAPI cache path is invalid: {_one_line(exc)}")
        return 1

    if args.offline:
        cached_version = read_version(cache_path)
        if cached_version is None:
            print(f"ERROR: no valid cached Mist OpenAPI document at {cache_path}")
            return 1
        print(f"Mist OpenAPI cache: version {cached_version} at {cache_path} (offline)")
        return 0

    try:
        refresh_result = refresh_spec(cache_path)
    except RefreshError as exc:
        cached_version = read_version(cache_path)
        if cached_version is None:
            fallback = "no valid cached document is available"
        else:
            fallback = f"cached version {cached_version} remains available"
        print(f"ERROR: Mist OpenAPI refresh failed: {_one_line(exc)}; {fallback}")
        return 1

    message = (
        f"Mist OpenAPI refreshed: version {refresh_result.version} at {cache_path}"
    )
    if refresh_result.metadata_warning:
        message += f"; WARNING: {refresh_result.metadata_warning}"
    print(message)
    if _update_check_enabled():
        notice = check_for_skill_update()
        if notice is not None:
            print(notice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
