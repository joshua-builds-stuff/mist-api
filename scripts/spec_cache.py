#!/usr/bin/env python3
"""Locate and validate the per-user Mist OpenAPI cache."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SPEC_FILENAME = "mist-api-openapi31.json"
MAX_SPEC_BYTES = 64 * 1024 * 1024
_INSTALL_ROOT = Path(__file__).resolve().parent.parent
_OPENAPI_3_VERSION = re.compile(r"^3\.[0-9]+(?:\.[0-9]+)?(?:[-+][0-9A-Za-z.-]+)?$")


class SpecValidationError(ValueError):
    """Raised when a file is not a usable Mist OpenAPI document."""


def _absolute_environment_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _default_cache_root(
    *,
    platform: str | None = None,
    os_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform-native cache root without creating it."""
    platform = sys.platform if platform is None else platform
    os_name = os.name if os_name is None else os_name
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home

    if os_name == "nt":
        local_app_data = _absolute_environment_path(environ.get("LOCALAPPDATA"))
        return local_app_data or home / "AppData" / "Local"
    if platform == "darwin":
        return home / "Library" / "Caches"
    xdg_cache_home = _absolute_environment_path(environ.get("XDG_CACHE_HOME"))
    return xdg_cache_home or home / ".cache"


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def resolve_spec_path(explicit: str | None = None) -> Path:
    """Resolve the external cache file using CLI, environment, then defaults.

    An explicit CLI value takes precedence over ``MIST_OPENAPI_PATH``. Either
    override must be an absolute file path. Paths inside the installed skill
    are rejected so a refresh can never mutate the installation. This function
    only computes a path; it does not touch the filesystem.
    """
    override = explicit if explicit is not None else os.environ.get("MIST_OPENAPI_PATH")
    if override is not None:
        path = Path(override).expanduser()
        if not path.is_absolute():
            source = (
                "explicit spec path" if explicit is not None else "MIST_OPENAPI_PATH"
            )
            raise ValueError(f"{source} must be an absolute file path")
    else:
        path = _default_cache_root() / "mist-api" / SPEC_FILENAME

    if _is_within(path, _INSTALL_ROOT):
        raise ValueError("the Mist OpenAPI cache must be outside the installed skill")
    return path


def validate_document(document: Any) -> str:
    if not isinstance(document, dict):
        raise SpecValidationError("document root must be a JSON object")

    openapi = document.get("openapi")
    if not isinstance(openapi, str) or not _OPENAPI_3_VERSION.fullmatch(openapi):
        raise SpecValidationError("openapi must identify a 3.x document")

    info = document.get("info")
    if not isinstance(info, dict):
        raise SpecValidationError("info must be an object")
    title = info.get("title")
    if not isinstance(title, str) or "mist" not in title.casefold():
        raise SpecValidationError("info.title must identify a Mist API document")
    version = info.get("version")
    if not isinstance(version, str) or not version.strip():
        raise SpecValidationError("info.version must be a non-empty string")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecValidationError("paths must be a non-empty object")
    if not any(
        isinstance(key, str) and key.startswith("/") and isinstance(value, dict)
        for key, value in paths.items()
    ):
        raise SpecValidationError("paths must contain at least one API path")

    components = document.get("components")
    if not isinstance(components, dict):
        raise SpecValidationError("components must be an object")
    schemas = components.get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        raise SpecValidationError("components.schemas must be a non-empty object")
    if not any(
        isinstance(name, str) and isinstance(schema, dict)
        for name, schema in schemas.items()
    ):
        raise SpecValidationError("components.schemas must contain a schema object")

    return version.strip()


def read_json_document(
    path: str | os.PathLike[str], *, max_bytes: int = MAX_SPEC_BYTES
) -> Any:
    """Bound the actual read even when a file changes after stat."""
    spec_path = Path(path)
    if spec_path.stat().st_size > max_bytes:
        raise SpecValidationError(f"document exceeds the {max_bytes}-byte size limit")
    with spec_path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise SpecValidationError(f"document exceeds the {max_bytes}-byte size limit")
    decoded = raw.decode("utf-8")
    del raw
    try:
        return json.loads(decoded)
    except json.JSONDecodeError:
        raise
    except (RecursionError, ValueError) as exc:
        raise SpecValidationError("JSON document exceeds parser limits") from exc


def validate_spec(path: str | os.PathLike[str]) -> str:
    """Validate an OpenAPI JSON file and return its advertised version."""
    try:
        document = read_json_document(path)
    except SpecValidationError:
        raise
    except json.JSONDecodeError as exc:
        raise SpecValidationError("document is not valid JSON") from exc
    except UnicodeError as exc:
        raise SpecValidationError("document is not valid UTF-8 JSON") from exc
    except OSError as exc:
        raise SpecValidationError(f"cannot read document: {exc}") from exc
    return validate_document(document)


def read_version(path: str | os.PathLike[str]) -> str | None:
    """Return the version of a valid cached spec, or ``None`` if unusable."""
    try:
        return validate_spec(path)
    except (SpecValidationError, TypeError, ValueError):
        return None
