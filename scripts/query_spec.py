#!/usr/bin/env python3
"""Query a Mist OpenAPI document without putting the whole spec in context.

Examples:
    python scripts/query_spec.py info
    python scripts/query_spec.py find wlans
    python scripts/query_spec.py show GET /api/v1/orgs/{org_id}/wlans
    python scripts/query_spec.py operation GET /api/v1/orgs/{org_id}/wlans
    python scripts/query_spec.py schema Wlan

Use ``--spec`` before the command to select a particular OpenAPI JSON file.
Without it, the shared spec cache resolver honors ``MIST_OPENAPI_PATH`` and
then uses the per-user cache.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .spec_cache import MAX_SPEC_BYTES, SpecValidationError, validate_document
except ImportError:  # Direct execution: ``python scripts/query_spec.py``.
    from spec_cache import (  # type: ignore[no-redef]
        MAX_SPEC_BYTES,
        SpecValidationError,
        validate_document,
    )

METHODS = ("get", "post", "put", "delete", "patch", "head", "options", "trace")
DEFAULT_LIMIT = 20
DEFAULT_MAX_CHARS = 6_000
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 50_000
MAX_REF_SUMMARY_DEPTH = 4
MAX_REF_SUMMARY_BRANCHES = 5
MAX_EXPANSION_NODES = 2_500
MAX_EXPANSION_PROPERTIES = 1_200

_SENSITIVE_NAME = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|authorization|community|cookie|credential|passphrase|"
    r"password|private[_-]?key|psk|secret|session|token)(?:$|[_-])",
    re.IGNORECASE,
)


class QueryError(Exception):
    """A concise, user-facing query failure."""


@dataclass
class ExpansionBudget:
    """Bound the structure built while recursively expanding schemas."""

    max_nodes: int
    max_properties: int
    nodes: int = 0
    properties: int = 0
    reasons: set[str] = field(default_factory=set)

    def claim_node(self) -> bool:
        if self.nodes >= self.max_nodes:
            self.reasons.add("schema-node-budget")
            return False
        self.nodes += 1
        return True

    def claim_property(self) -> bool:
        if self.properties >= self.max_properties:
            self.reasons.add("schema-property-budget")
            return False
        self.properties += 1
        return True


@dataclass(frozen=True)
class CommandOutput:
    value: Any
    is_json: bool = False
    truncation_reasons: tuple[str, ...] = ()


def _bounded_int(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return number

    return parse


limit_type = _bounded_int("limit", 1, 200)
depth_type = _bounded_int("max depth", 0, 5)
max_chars_type = _bounded_int("max chars", MIN_MAX_CHARS, MAX_MAX_CHARS)


def _resolve_spec_path(explicit: str | None) -> Path:
    try:
        try:
            from spec_cache import resolve_spec_path
        except ImportError:
            from scripts.spec_cache import resolve_spec_path
    except ImportError as exc:
        raise QueryError("OpenAPI spec resolver is unavailable") from exc

    try:
        return Path(resolve_spec_path(explicit)).expanduser()
    except (OSError, TypeError, ValueError) as exc:
        raise QueryError(f"Could not resolve OpenAPI spec path: {exc}") from exc


def load_spec(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_SPEC_BYTES:
            raise QueryError(
                f"OpenAPI spec exceeds the {MAX_SPEC_BYTES}-byte size limit: "
                f"{_display_path(path)}"
            )
        with path.open("r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except FileNotFoundError as exc:
        raise QueryError(f"OpenAPI spec not found: {_display_path(path)}") from exc
    except json.JSONDecodeError as exc:
        raise QueryError(
            f"Malformed OpenAPI JSON: {_display_path(path)} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    except QueryError:
        raise
    except (OSError, UnicodeError) as exc:
        raise QueryError(
            f"Could not read OpenAPI spec: {_display_path(path)} ({exc})"
        ) from exc

    try:
        validate_document(spec)
    except SpecValidationError as exc:
        raise QueryError(
            f"OpenAPI spec is not a valid Mist document: {_display_path(path)} ({exc})"
        ) from exc
    return spec


def cmd_info(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    operation_count = sum(
        1
        for path_item in paths.values()
        if isinstance(path_item, dict)
        for method in path_item
        if method.lower() in METHODS
    )
    schemas = spec.get("components", {}).get("schemas", {})
    schemes = spec.get("components", {}).get("securitySchemes", {})
    lines = [
        f"title:    {info.get('title')}",
        f"version:  {info.get('version')}",
        f"openapi:  {spec.get('openapi')}",
        f"paths:    {len(paths)}",
        f"ops:      {operation_count}",
        f"schemas:  {len(schemas) if isinstance(schemas, dict) else 0}",
        "servers (regional clouds):",
    ]
    servers = spec.get("servers") if isinstance(spec.get("servers"), list) else []
    lines.extend(
        f"  {server.get('url')}"
        for server in servers
        if isinstance(server, dict) and server.get("url")
    )
    scheme_names = sorted(schemes) if isinstance(schemes, dict) else []
    lines.append(f"security schemes: {', '.join(scheme_names)}")
    return CommandOutput("\n".join(lines))


def cmd_find(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    """Search paths, summaries, descriptions, operation IDs, and tags."""
    terms = args.term.casefold().split()
    normalized_query = re.sub(r"[^a-z0-9]", "", args.term.casefold())
    matches: list[tuple[int, int, str, str, str, bool]] = []
    for path, path_item in sorted(spec.get("paths", {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method in METHODS:
            raw_operation = path_item.get(method)
            if not isinstance(raw_operation, dict):
                continue
            operation = _resolve_object_ref(spec, raw_operation)
            tags = (
                operation.get("tags") if isinstance(operation.get("tags"), list) else []
            )
            haystack = " ".join(
                (
                    path,
                    _text(operation.get("summary")),
                    _text(operation.get("description")),
                    _text(operation.get("operationId")),
                    " ".join(_text(tag) for tag in tags),
                )
            ).casefold()
            if not all(term in haystack for term in terms):
                continue
            detail = _text(operation.get("summary") or operation.get("operationId"))
            operation_id = _text(operation.get("operationId")).casefold()
            normalized_id = re.sub(r"[^a-z0-9]", "", operation_id)
            score = sum(8 for term in terms if term in path.casefold())
            score += sum(3 for term in terms if term in operation_id)
            score += sum(
                1
                for term in terms
                if term in _text(operation.get("description")).casefold()
            )
            if normalized_query and normalized_query == normalized_id:
                score += 100
            matches.append(
                (
                    score,
                    len(path),
                    method,
                    path,
                    detail,
                    bool(operation.get("deprecated")),
                )
            )

    if not matches:
        return CommandOutput(f"No paths matching '{args.term}'")
    matches.sort(key=lambda item: (-item[0], item[1], item[3], item[2]))
    lines = []
    for _score, _length, method, path, detail, deprecated in matches[: args.limit]:
        marker = " [DEPRECATED]" if deprecated else ""
        suffix = f"  - {detail}" if detail else ""
        lines.append(f"{method.upper():<7} {path}{marker}{suffix}")
    if len(matches) > args.limit:
        lines.append(
            f"... ({len(matches) - args.limit} more; raise --limit to show them)"
        )
    return CommandOutput("\n".join(lines))


def cmd_show(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    """Show one operation's summary, parameters, body, and responses."""
    path, path_item, operation = _get_operation(spec, args.method, args.path)
    lines = [f"{args.method.upper()} {path}"]
    if operation.get("operationId"):
        lines.append(f"operationId: {operation['operationId']}")
    if operation.get("tags"):
        lines.append(f"tags: {', '.join(map(str, operation['tags']))}")
    lines.append(f"deprecated: {str(bool(operation.get('deprecated'))).lower()}")
    security = _effective_security(spec, operation)
    lines.append(
        f"security: {json.dumps(security, ensure_ascii=False, sort_keys=True)}"
    )
    if operation.get("summary"):
        lines.append(f"summary: {_one_line(operation['summary'], 400)}")
    if operation.get("description"):
        lines.append(f"description: {_one_line(operation['description'], 400)}")

    parameters = _merged_parameters(spec, path_item, operation)
    if parameters:
        lines.extend(("", "parameters:"))
        for parameter in parameters:
            schema = parameter.get("schema")
            schema = schema if isinstance(schema, dict) else {}
            parameter_type = schema.get(
                "type", _ref_name(schema.get("$ref", "")) or "?"
            )
            required = "required" if parameter.get("required") else "optional"
            description = (
                f": {_one_line(parameter['description'], 80)}"
                if parameter.get("description")
                else ""
            )
            lines.append(
                f"  - {parameter.get('name')} ({parameter.get('in')}, "
                f"{parameter_type}, {required}){description}"
            )

    body = _resolve_object_ref(spec, operation.get("requestBody", {}))
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    if content:
        lines.extend(("", "requestBody:"))
        for content_type, media in sorted(content.items()):
            media = media if isinstance(media, dict) else {}
            schema = (
                media.get("schema") if isinstance(media.get("schema"), dict) else {}
            )
            named = _schema_ref_summary(schema)
            lines.append(
                f"  {content_type} -> schema: {named or _schema_label(schema)}"
            )

    responses = operation.get("responses")
    if isinstance(responses, dict) and responses:
        lines.extend(("", "responses:"))
        for code, raw_response in sorted(
            responses.items(), key=lambda item: str(item[0])
        ):
            response = _resolve_object_ref(spec, raw_response)
            description = _one_line(response.get("description", ""), 160)
            schema_ref = ""
            response_content = response.get("content")
            if isinstance(response_content, dict):
                for media in response_content.values():
                    if isinstance(media, dict) and isinstance(
                        media.get("schema"), dict
                    ):
                        schema_ref = _schema_ref_summary(media["schema"])
                        break
            suffix = f"  -> {schema_ref}" if schema_ref else ""
            lines.append(f"  {code}: {description}{suffix}")
    return CommandOutput("\n".join(lines))


def cmd_schema(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    schemas = spec.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        raise QueryError("This OpenAPI spec has no component schemas")
    name = next(
        (key for key in schemas if key.casefold() == args.name.casefold()), args.name
    )
    if name not in schemas:
        matches = sorted(
            key for key in schemas if args.name.casefold() in key.casefold()
        )
        if not matches:
            raise QueryError(f"No schema named or matching '{args.name}'")
        if len(matches) > 1:
            shown = matches[: args.limit]
            more = len(matches) - len(shown)
            lines = [
                f"Multiple matches for '{args.name}':",
                *(f"  {item}" for item in shown),
            ]
            if more:
                lines.append(f"... ({more} more; narrow the name or raise --limit)")
            return CommandOutput("\n".join(lines))
        name = matches[0]

    budget = _expansion_budget()
    if args.property:
        located = _find_schema_property(spec, schemas[name], args.property)
        if located is None:
            raise QueryError(f"Schema '{name}' has no property named '{args.property}'")
        value = {
            "component": name,
            "property": _property_match_name(located),
            **_render_property_match(spec, located, args.max_depth, budget),
        }
    else:
        document = _expand_schema(spec, schemas[name], args.max_depth, budget=budget)
        value = {"component": name, "schema": document}
    return CommandOutput(
        value,
        is_json=True,
        truncation_reasons=tuple(sorted(budget.reasons)),
    )


def cmd_operation(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    """Emit one operation with compact, recursively expanded schemas."""
    path, path_item, operation = _get_operation(spec, args.method, args.path)
    budget = _expansion_budget()
    result: dict[str, Any] = {
        "method": args.method.upper(),
        "path": path,
        "operationId": operation.get("operationId"),
        "summary": _optional_concise_text(operation.get("summary"), 400),
        "description": _optional_concise_text(operation.get("description"), 800),
        "tags": operation.get("tags", []),
        "deprecated": bool(operation.get("deprecated")),
        "security": _effective_security(spec, operation),
        "parameters": [],
        "requestBody": {},
        "responses": {},
    }
    for parameter in _merged_parameters(spec, path_item, operation):
        result["parameters"].append(
            {
                "name": parameter.get("name"),
                "in": parameter.get("in"),
                "required": parameter.get("required", False),
                "deprecated": bool(parameter.get("deprecated")),
                "description": _optional_concise_text(
                    parameter.get("description"), 300
                ),
                "schema": _expand_schema(
                    spec,
                    parameter.get("schema", {}),
                    args.max_depth,
                    budget=budget,
                    context_name=_text(parameter.get("name")),
                ),
            }
        )

    body = _resolve_object_ref(spec, operation.get("requestBody", {}))
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    for content_type, media in sorted(content.items()):
        media = media if isinstance(media, dict) else {}
        result["requestBody"][content_type] = _expand_schema(
            spec, media.get("schema", {}), args.max_depth, budget=budget
        )

    responses = operation.get("responses")
    responses = responses if isinstance(responses, dict) else {}
    for code, raw_response in sorted(responses.items(), key=lambda item: str(item[0])):
        response = _resolve_object_ref(spec, raw_response)
        item: dict[str, Any] = {
            "description": _optional_concise_text(response.get("description"), 300),
            "content": {},
        }
        response_content = response.get("content")
        response_content = (
            response_content if isinstance(response_content, dict) else {}
        )
        for content_type, media in sorted(response_content.items()):
            media = media if isinstance(media, dict) else {}
            item["content"][content_type] = _expand_schema(
                spec, media.get("schema", {}), args.max_depth, budget=budget
            )
        result["responses"][str(code)] = item

    return CommandOutput(
        result,
        is_json=True,
        truncation_reasons=tuple(sorted(budget.reasons)),
    )


def cmd_tags(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    counts: dict[str, int] = {}
    for path_item in spec.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method in METHODS:
            raw_operation = path_item.get(method)
            if not isinstance(raw_operation, dict):
                continue
            operation = _resolve_object_ref(spec, raw_operation)
            tags = (
                operation.get("tags") if isinstance(operation.get("tags"), list) else []
            )
            for tag in tags:
                tag = _text(tag)
                counts[tag] = counts.get(tag, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"{count:>4}  {tag}" for tag, count in ordered[: args.limit]]
    if len(ordered) > args.limit:
        lines.append(
            f"... ({len(ordered) - args.limit} more; raise --limit to show them)"
        )
    return CommandOutput("\n".join(lines))


def cmd_tag(spec: dict[str, Any], args: argparse.Namespace) -> CommandOutput:
    needle = args.name.casefold()
    matches: list[str] = []
    for path, path_item in sorted(spec.get("paths", {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method in METHODS:
            raw_operation = path_item.get(method)
            if not isinstance(raw_operation, dict):
                continue
            operation = _resolve_object_ref(spec, raw_operation)
            tags = (
                operation.get("tags") if isinstance(operation.get("tags"), list) else []
            )
            if any(needle == _text(tag).casefold() for tag in tags):
                marker = " [DEPRECATED]" if operation.get("deprecated") else ""
                summary = (
                    f"  - {operation['summary']}" if operation.get("summary") else ""
                )
                matches.append(f"{method.upper():<7} {path}{marker}{summary}")
    shown = matches[: args.limit]
    if len(matches) > args.limit:
        shown.append(
            f"... ({len(matches) - args.limit} more; raise --limit to show them)"
        )
    if not shown:
        shown.append(f"No operations tagged '{args.name}'")
    return CommandOutput("\n".join(shown))


def _effective_security(spec: dict[str, Any], operation: dict[str, Any]) -> Any:
    security = (
        operation["security"] if "security" in operation else spec.get("security", [])
    )
    return security if isinstance(security, list) else []


def _merged_parameters(
    spec: dict[str, Any], path_item: dict[str, Any], operation: dict[str, Any]
) -> list[dict[str, Any]]:
    """Merge path-level parameters with operation-level overrides."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for owner in (path_item, operation):
        raw_parameters = owner.get("parameters")
        if not isinstance(raw_parameters, list):
            continue
        for raw_parameter in raw_parameters:
            parameter = _resolve_object_ref(spec, raw_parameter)
            key = (_text(parameter.get("in")), _text(parameter.get("name")))
            if key != ("", ""):
                merged[key] = parameter
    return [merged[key] for key in sorted(merged)]


def _ref_name(ref: Any) -> str:
    return _text(ref).rsplit("/", 1)[-1] if ref else ""


def _resolve_pointer(document: Any, pointer: str) -> Any:
    if not pointer.startswith("#/"):
        return None
    value = document
    for encoded_part in pointer[2:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _resolve_object_ref(
    spec: dict[str, Any], value: Any, seen: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    ref = value.get("$ref")
    if not isinstance(ref, str) or ref in seen:
        return value
    resolved = _resolve_pointer(spec, ref)
    if not isinstance(resolved, dict):
        return value
    base = _resolve_object_ref(spec, resolved, seen | {ref})
    return {**base, **{key: item for key, item in value.items() if key != "$ref"}}


@dataclass(frozen=True)
class _PropertyMatch:
    name: str
    schema: dict[str, Any]
    required: bool


@dataclass(frozen=True)
class _PropertyVariants:
    """A property that differs across oneOf/anyOf branches."""

    keyword: str
    discriminator: str | None
    entries: tuple[tuple[str, _PropertyMatch | _PropertyVariants | None], ...]


_PropertyLookup = _PropertyMatch | _PropertyVariants

_LOWER_BOUND_KEYWORDS = frozenset(
    {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
)
_UPPER_BOUND_KEYWORDS = frozenset(
    {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}
)
_ANNOTATION_KEYWORDS = frozenset({"title", "description", "example", "examples"})


def _find_schema_property(
    spec: dict[str, Any],
    schema: Any,
    requested_name: str,
    seen_refs: frozenset[str] = frozenset(),
) -> _PropertyLookup | None:
    """Find one exact property through local refs and composition keywords.

    Own properties, ``$ref`` targets and ``allOf`` branches all apply, so their
    matches are merged. ``oneOf``/``anyOf`` matches are reported per variant.
    """

    if not isinstance(schema, dict):
        return None

    found: list[_PropertyLookup] = []

    properties = schema.get("properties")
    if isinstance(properties, dict):
        matches = [
            name
            for name in properties
            if isinstance(name, str) and name.casefold() == requested_name.casefold()
        ]
        if len(matches) > 1:
            raise QueryError(
                f"Property name '{requested_name}' is ambiguous by letter case"
            )
        if matches:
            name = matches[0]
            property_schema = properties[name]
            if not isinstance(property_schema, dict):
                raise QueryError(f"Property '{name}' does not contain a schema object")
            required = schema.get("required")
            found.append(
                _PropertyMatch(
                    name,
                    property_schema,
                    isinstance(required, list) and name in required,
                )
            )

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in seen_refs:
        resolved = _resolve_pointer(spec, ref)
        located = _find_schema_property(
            spec,
            resolved,
            requested_name,
            seen_refs | {ref},
        )
        if located is not None:
            found.append(located)

    branches = schema.get("allOf")
    if isinstance(branches, list):
        for branch in branches:
            located = _find_schema_property(spec, branch, requested_name, seen_refs)
            if located is not None:
                found.append(located)

    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        labels = _variant_labels(schema, keyword, branches)
        entries = tuple(
            (label, _find_schema_property(spec, branch, requested_name, seen_refs))
            for label, branch in zip(labels, branches)
        )
        if any(entry is not None for _, entry in entries):
            found.append(
                _property_variants(keyword, _discriminator_name(schema), entries)
            )

    if not found:
        return None
    combined = found[0]
    for item in found[1:]:
        combined = _combine_property_lookups(spec, combined, item)
    return combined


def _discriminator_name(schema: dict[str, Any]) -> str | None:
    discriminator = schema.get("discriminator")
    if isinstance(discriminator, dict):
        name = discriminator.get("propertyName")
        if isinstance(name, str):
            return name
    return None


def _variant_labels(
    schema: dict[str, Any], keyword: str, branches: list[Any]
) -> list[str]:
    discriminator = schema.get("discriminator")
    mapping = discriminator.get("mapping") if isinstance(discriminator, dict) else None
    labels: list[str] = []
    for index, branch in enumerate(branches):
        ref = branch.get("$ref") if isinstance(branch, dict) else None
        label = ""
        if isinstance(ref, str) and isinstance(mapping, dict):
            label = next(
                (
                    str(key)
                    for key, target in mapping.items()
                    if isinstance(target, str)
                    and (target == ref or target == _ref_name(ref))
                ),
                "",
            )
        if not label and isinstance(ref, str):
            label = _ref_name(ref)
        if not label or label in labels:
            label = f"{keyword}[{index}]"
        labels.append(label)
    return labels


def _property_variants(
    keyword: str,
    discriminator: str | None,
    entries: tuple[tuple[str, _PropertyLookup | None], ...],
) -> _PropertyLookup:
    first = entries[0][1] if entries else None
    if isinstance(first, _PropertyMatch) and all(
        isinstance(entry, _PropertyMatch)
        and entry.schema == first.schema
        and entry.required == first.required
        for _, entry in entries
    ):
        return first
    return _PropertyVariants(keyword, discriminator, entries)


def _combine_property_lookups(
    spec: dict[str, Any], first: _PropertyLookup, second: _PropertyLookup
) -> _PropertyLookup:
    """Combine two lookups that both apply to the same instance."""

    if isinstance(first, _PropertyVariants):
        return _property_variants(
            first.keyword,
            first.discriminator,
            tuple(
                (
                    label,
                    second
                    if entry is None
                    else _combine_property_lookups(spec, entry, second),
                )
                for label, entry in first.entries
            ),
        )
    if isinstance(second, _PropertyVariants):
        return _property_variants(
            second.keyword,
            second.discriminator,
            tuple(
                (
                    label,
                    first
                    if entry is None
                    else _combine_property_lookups(spec, first, entry),
                )
                for label, entry in second.entries
            ),
        )
    if first.schema == second.schema:
        schema = first.schema
    else:
        schema = _merge_schema_constraints(
            _resolve_object_ref(spec, first.schema),
            _resolve_object_ref(spec, second.schema),
        )
    return _PropertyMatch(first.name, schema, first.required or second.required)


def _merge_schema_constraints(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
    """Merge two schemas that must both hold, keeping unmergeable parts."""

    merged = dict(first)
    leftovers: dict[str, Any] = {}
    for key, value in second.items():
        if key not in merged:
            merged[key] = value
            continue
        current = merged[key]
        if current == value or key in _ANNOTATION_KEYWORDS:
            continue
        if key == "type":
            current_types = current if isinstance(current, list) else [current]
            value_types = value if isinstance(value, list) else [value]
            shared = [item for item in current_types if item in value_types]
            if shared:
                merged[key] = shared[0] if len(shared) == 1 else shared
                continue
        elif key == "required" and isinstance(current, list) and isinstance(
            value, list
        ):
            merged[key] = [*current, *(item for item in value if item not in current)]
            continue
        elif key == "enum" and isinstance(current, list) and isinstance(value, list):
            shared = [item for item in current if item in value]
            if shared:
                merged[key] = shared
                continue
        elif key == "nullable" and isinstance(current, bool) and isinstance(
            value, bool
        ):
            merged[key] = current and value
            continue
        elif _is_number(current) and _is_number(value):
            if key in _LOWER_BOUND_KEYWORDS:
                merged[key] = max(current, value)
                continue
            if key in _UPPER_BOUND_KEYWORDS:
                merged[key] = min(current, value)
                continue
        leftovers[key] = value
    if leftovers:
        existing = merged.get("allOf")
        merged["allOf"] = [*(existing if isinstance(existing, list) else []), leftovers]
    return merged


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _property_match_name(located: _PropertyLookup) -> str:
    if isinstance(located, _PropertyMatch):
        return located.name
    return next(
        _property_match_name(entry)
        for _, entry in located.entries
        if entry is not None
    )


def _render_property_match(
    spec: dict[str, Any],
    located: _PropertyLookup,
    depth: int,
    budget: ExpansionBudget,
) -> dict[str, Any]:
    if isinstance(located, _PropertyMatch):
        return {
            "required": located.required,
            "schema": _expand_schema(
                spec,
                located.schema,
                depth,
                budget=budget,
                context_name=located.name,
            ),
        }
    return {
        "composition": located.keyword,
        "discriminator": located.discriminator,
        "variants": {
            label: _render_property_match(spec, entry, depth, budget)
            for label, entry in located.entries
            if entry is not None
        },
        "absentFrom": [label for label, entry in located.entries if entry is None],
    }


def _expansion_budget() -> ExpansionBudget:
    # This only bounds build work; _render_json fits the output to --max-chars.
    # Scaling it down with --max-chars starved trailing properties entirely.
    return ExpansionBudget(
        max_nodes=MAX_EXPANSION_NODES, max_properties=MAX_EXPANSION_PROPERTIES
    )


def _expand_schema(
    spec: dict[str, Any],
    schema: Any,
    depth: int,
    seen: frozenset[str] = frozenset(),
    *,
    budget: ExpansionBudget,
    context_name: str = "",
) -> Any:
    """Expand local refs while preserving siblings and a strict build budget."""
    if not budget.claim_node():
        return {"x-query-truncated": "schema-node-budget"}
    if not isinstance(schema, dict):
        return _bounded_copy(schema, budget)

    ref = schema.get("$ref")
    siblings = {key: value for key, value in schema.items() if key != "$ref"}
    if isinstance(ref, str):
        resolved = _resolve_pointer(spec, ref)
        if depth <= 0 or ref in seen or not isinstance(resolved, dict):
            base: dict[str, Any] = {"$ref": ref}
        else:
            expanded = _expand_schema(
                spec,
                resolved,
                depth - 1,
                seen | {ref},
                budget=budget,
                context_name=context_name,
            )
            base = expanded if isinstance(expanded, dict) else {"value": expanded}
            base = {"x-expanded-from": ref, **base}
        if siblings:
            sibling_values = _expand_schema_fields(
                spec,
                siblings,
                depth,
                seen,
                budget=budget,
                context_name=context_name,
            )
            base.update(sibling_values)
        return base

    return _expand_schema_fields(
        spec,
        schema,
        depth,
        seen,
        budget=budget,
        context_name=context_name,
    )


def _expand_schema_fields(
    spec: dict[str, Any],
    schema: dict[str, Any],
    depth: int,
    seen: frozenset[str],
    *,
    budget: ExpansionBudget,
    context_name: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    metadata_keys = (
        "title",
        "type",
        "format",
        "description",
        "enum",
        "const",
        "required",
        "nullable",
        "readOnly",
        "writeOnly",
        "deprecated",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "discriminator",
        "contentMediaType",
        "contentEncoding",
    )
    for key in metadata_keys:
        if key not in schema:
            continue
        if key == "const" and _is_sensitive_name(context_name):
            continue
        result[key] = _bounded_copy(schema[key], budget)

    if "default" in schema and _safe_default(schema["default"], context_name):
        result["default"] = schema["default"]
    # Examples are intentionally omitted: they are often large and may contain secrets.

    properties = schema.get("properties")
    if isinstance(properties, dict):
        expanded_properties: dict[str, Any] = {}
        ordered_properties = sorted(properties.items())
        for index, (name, value) in enumerate(ordered_properties):
            if not budget.claim_property():
                expanded_properties["x-query-truncated"] = {
                    "reason": "schema-property-budget",
                    "omitted": len(ordered_properties) - index,
                }
                break
            expanded_properties[name] = _expand_schema(
                spec,
                value,
                depth,
                seen,
                budget=budget,
                context_name=name,
            )
        result["properties"] = expanded_properties

    mapping_keywords = ("patternProperties", "dependentSchemas")
    for keyword in mapping_keywords:
        mapping = schema.get(keyword)
        if not isinstance(mapping, dict):
            continue
        result[keyword] = {}
        for name, value in sorted(mapping.items()):
            if not budget.claim_property():
                result[keyword]["x-query-truncated"] = "schema-property-budget"
                break
            result[keyword][name] = _expand_schema(
                spec, value, depth, seen, budget=budget, context_name=name
            )

    schema_keywords = (
        "items",
        "contains",
        "additionalProperties",
        "unevaluatedProperties",
        "propertyNames",
        "not",
        "if",
        "then",
        "else",
    )
    for keyword in schema_keywords:
        value = schema.get(keyword)
        if isinstance(value, dict):
            result[keyword] = _expand_schema(
                spec, value, depth, seen, budget=budget, context_name=context_name
            )
        elif keyword in schema and isinstance(value, bool):
            result[keyword] = value

    for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
        values = schema.get(keyword)
        if not isinstance(values, list):
            continue
        expanded_values = []
        for value in values:
            if budget.nodes >= budget.max_nodes:
                budget.reasons.add("schema-node-budget")
                expanded_values.append({"x-query-truncated": "schema-node-budget"})
                break
            expanded_values.append(
                _expand_schema(
                    spec, value, depth, seen, budget=budget, context_name=context_name
                )
            )
        result[keyword] = expanded_values
    return result


def _bounded_copy(value: Any, budget: ExpansionBudget) -> Any:
    """Copy metadata into a small JSON-safe form under the node budget."""
    if not budget.claim_node():
        return {"x-query-truncated": "schema-node-budget"}
    if isinstance(value, str):
        return _one_line(value, 500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        result = []
        for item in value:
            if budget.nodes >= budget.max_nodes:
                budget.reasons.add("schema-node-budget")
                result.append({"x-query-truncated": "schema-node-budget"})
                break
            result.append(_bounded_copy(item, budget))
        return result
    if isinstance(value, dict):
        result = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if budget.nodes >= budget.max_nodes:
                budget.reasons.add("schema-node-budget")
                result["x-query-truncated"] = "schema-node-budget"
                break
            result[str(key)] = _bounded_copy(item, budget)
        return result
    return _one_line(str(value), 200)


def _safe_default(value: Any, context_name: str) -> bool:
    if _is_sensitive_name(context_name) or isinstance(value, (dict, list)):
        return False
    return not isinstance(value, str) or len(value) <= 128


def _is_sensitive_name(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return bool(_SENSITIVE_NAME.search(normalized))


def _get_operation(
    spec: dict[str, Any], method: str, requested_path: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    paths = spec.get("paths") if isinstance(spec.get("paths"), dict) else {}
    path = requested_path
    raw_path_item = paths.get(path)
    if raw_path_item is None:
        alternative = f"/api/v1{path}" if not path.startswith("/api/v1") else path
        raw_path_item = paths.get(alternative)
        if raw_path_item is not None:
            path = alternative
    if not isinstance(raw_path_item, dict):
        raise QueryError(f"Path not found: {requested_path} (try: find <part>)")
    path_item = _resolve_object_ref(spec, raw_path_item)
    raw_operation = path_item.get(method.casefold())
    if not isinstance(raw_operation, dict):
        available = ", ".join(item.upper() for item in METHODS if item in path_item)
        raise QueryError(
            f"{method.upper()} not defined on {path}. Available: {available or 'none'}"
        )
    return path, path_item, _resolve_object_ref(spec, raw_operation)


def _schema_ref_summary(schema: Any, depth: int = 0) -> str:
    """Name the component schemas behind a ref, composition, or array."""
    if not isinstance(schema, dict) or depth > MAX_REF_SUMMARY_DEPTH:
        return ""
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref:
        return _ref_name(ref)
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        names = [
            name
            for name in (_schema_ref_summary(branch, depth + 1) for branch in branches)
            if name
        ]
        if names:
            shown = names[:MAX_REF_SUMMARY_BRANCHES]
            more = len(names) - len(shown)
            extra = f", +{more} more" if more else ""
            return f"{keyword}[{', '.join(shown)}{extra}]"
    items = schema.get("items")
    if isinstance(items, dict):
        item_name = _schema_ref_summary(items, depth + 1)
        if item_name:
            return f"array[{item_name}]"
    return ""


def _schema_label(schema: dict[str, Any]) -> str:
    if not schema:
        return "{}"
    if schema.get("type"):
        return _text(schema["type"])
    return "inline schema"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _display_path(path: Path, limit: int = 240) -> str:
    return _one_line(path, limit)


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(_text(value).split())
    return (
        text
        if len(text) <= limit
        else f"{text[: max(0, limit - 14)].rstrip()}… [truncated]"
    )


def _optional_concise_text(value: Any, limit: int) -> str | None:
    return _one_line(value, limit) if value is not None else None


def _render_text(value: Any, max_chars: int) -> str:
    rendered = _text(value).rstrip() + "\n"
    if len(rendered) <= max_chars:
        return rendered
    marker = f"\n... [output truncated at {max_chars} characters]\n"
    prefix_length = max(0, max_chars - len(marker))
    return rendered[:prefix_length].rstrip() + marker


def _render_json(
    data: Any, max_chars: int, initial_reasons: tuple[str, ...] = ()
) -> str:
    reasons = list(dict.fromkeys(initial_reasons))
    envelope = {
        "_meta": {
            "max_chars": max_chars,
            "truncated": bool(reasons),
            "reasons": reasons,
        },
        "data": copy.deepcopy(data),
    }

    def render() -> str:
        return json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)

    rendered = render()
    if len(rendered) + 1 <= max_chars:
        return rendered + "\n"

    if "output-character-budget" not in reasons:
        reasons.append("output-character-budget")
    envelope["_meta"]["truncated"] = True
    envelope["_meta"]["reasons"] = reasons

    original = envelope["data"]
    steps = _reduction_steps(original)

    def fits(count: int) -> bool:
        envelope["data"] = _apply_reduction_steps(original, steps[:count])
        return len(render()) + 1 <= max_chars

    # Binary search for the shortest prefix of the plan that fits. ``high``
    # always refers to a prefix that fits, so the result is always in budget.
    if fits(len(steps)):
        low, high = 0, len(steps)
        while low < high:
            middle = (low + high) // 2
            if fits(middle):
                high = middle
            else:
                low = middle + 1
        envelope["data"] = _apply_reduction_steps(original, steps[:high])
    else:
        envelope["data"] = {"x-query-truncated": "output-character-budget"}

    rendered = render()
    if len(rendered) + 1 > max_chars:
        # The minimum allowed max_chars is large enough for this final envelope.
        envelope["data"] = None
        rendered = render()
    return rendered + "\n"


ReductionStep = tuple[str, tuple[Any, ...], Any]
_TRUNCATED_TEXT = "… [truncated]"
_MIN_TRUNCATED_STRING = 40
_KEPT_STRING_PREFIX = 24
# Short structural fields that identify a schema; kept when a node collapses.
_STRUCTURAL_KEYS = frozenset(("$ref", "format", "type", "x-expanded-from"))


def _reduction_steps(value: Any) -> list[ReductionStep]:
    """Plan output reductions from least to most destructive.

    Long free-text strings are shortened first (longest first). Next, nested
    schemas collapse to one-line labels such as ``"array[string]|null"``,
    deepest and trailing first. Labels are computed up front because deeper
    steps rewrite the children. Only then are trailing entries dropped from
    the outermost schema (e.g. its last properties), so property names
    outlive the details beneath them and the root is replaced only last.
    """
    strings: list[tuple[int, int, tuple[Any, ...]]] = []
    labelled: list[tuple[int, int, tuple[Any, ...], Any]] = []
    # Steps owned by a labelled node (or by the root, keyed ``None``).
    owned: dict[tuple[Any, ...] | None, list[tuple[int, int, ReductionStep]]] = {}
    order = 0

    def visit(
        item: Any,
        path: tuple[Any, ...],
        parent_key: Any,
        owner: tuple[Any, ...] | None,
        may_drop: bool,
    ) -> None:
        nonlocal order
        order += 1
        if isinstance(item, str):
            if len(item) > _MIN_TRUNCATED_STRING and parent_key not in _STRUCTURAL_KEYS:
                strings.append((-len(item), order, path))
            return
        if isinstance(item, dict):
            children = [(key, item[key]) for key in sorted(item)]
        elif isinstance(item, list):
            children = list(enumerate(item))
        else:
            return
        summary = _collapsed_summary(item)
        if path and item:
            if isinstance(summary, str):
                labelled.append((len(path), order, path, summary))
                # Dropping details is only worthwhile in the outermost schema;
                # nested schemas are cheaper to collapse to their label.
                may_drop = owner is None
                owner = path
            else:
                owned.setdefault(owner, []).append(
                    (len(path), order, ("collapse", path, summary))
                )
        for key, child in children:
            child_path = (*path, key)
            if _is_query_marker(key):
                continue
            if may_drop and isinstance(child, (dict, list)):
                owned.setdefault(owner, []).append(
                    (len(child_path), order, ("drop", child_path, None))
                )
            visit(child, child_path, key, owner, may_drop)

    visit(value, (), None, None, True)

    def owned_steps(owner: tuple[Any, ...] | None) -> list[ReductionStep]:
        entries = sorted(owned.get(owner, []), key=lambda e: (-e[0], -e[1]))
        return [step for _depth, _order, step in entries]

    steps: list[ReductionStep] = [
        ("shorten", path, None) for _length, _order, path in sorted(strings)
    ]
    for _depth, _order, path, summary in sorted(labelled, key=lambda e: (-e[0], -e[1])):
        steps.extend(owned_steps(path))
        steps.append(("collapse", path, summary))
    steps.extend(owned_steps(None))
    return steps


def _is_query_marker(key: Any) -> bool:
    return isinstance(key, str) and key.startswith("x-query-")


def _apply_reduction_steps(value: Any, steps: list[ReductionStep]) -> Any:
    document = copy.deepcopy(value)
    omitted: dict[tuple[Any, ...], int] = {}
    for action, path, summary in steps:
        parent = _container_at(document, path[:-1])
        key = path[-1]
        if parent is None or not _has_entry(parent, key):
            continue
        current = parent[key]
        if action == "shorten":
            if isinstance(current, str):
                parent[key] = current[:_KEPT_STRING_PREFIX].rstrip() + _TRUNCATED_TEXT
        elif action == "collapse":
            if _json_size(summary) < _json_size(current):
                parent[key] = copy.deepcopy(summary)
                omitted.pop(path, None)
        elif action == "drop":
            # Drops run trailing-first, so list indices of pending steps stay valid.
            del parent[key]
            omitted[path[:-1]] = omitted.get(path[:-1], 0) + 1
    for parent_path, count in omitted.items():
        parent = _container_at(document, parent_path)
        if isinstance(parent, list):
            parent.append({"x-query-omitted": count})
        elif isinstance(parent, dict):
            parent["x-query-omitted"] = count
    return document


def _collapsed_summary(value: Any) -> Any:
    label = _schema_label_text(value) if isinstance(value, dict) else ""
    if label:
        return label
    if isinstance(value, dict):
        return {"x-query-truncated": "output-character-budget"}
    return [{"x-query-truncated": "output-character-budget"}]


def _schema_label_text(schema: Any, depth: int = 0) -> str:
    """Describe a schema in one line, e.g. ``array[string]|null`` or ``site``."""
    if not isinstance(schema, dict) or depth > 3:
        return ""
    ref = schema.get("$ref") or schema.get("x-expanded-from")
    ref_name = _ref_name(ref) if isinstance(ref, str) else ""
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        types = [raw_type]
    elif isinstance(raw_type, list):
        types = [item for item in raw_type if isinstance(item, str)]
    else:
        types = []
    if "array" in types:
        item_label = _schema_label_text(schema.get("items"), depth + 1)
        if item_label:
            types[types.index("array")] = f"array[{item_label}]"
    type_label = "|".join(types)
    if ref_name:
        return (
            ref_name if type_label in ("", "object") else f"{ref_name} ({type_label})"
        )
    if type_label:
        return type_label
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        labels = [
            label
            for label in (_schema_label_text(item, depth + 1) for item in branches)
            if label
        ]
        if len(labels) == 1 and keyword == "allOf":
            return labels[0]
        if labels:
            more = ", …" if len(labels) > 3 else ""
            return f"{keyword}[{', '.join(labels[:3])}{more}]"
    return ""


def _container_at(root: Any, path: tuple[Any, ...]) -> Any:
    current = root
    for part in path:
        if not _has_entry(current, part):
            return None
        current = current[part]
    return current if isinstance(current, (dict, list)) else None


def _has_entry(container: Any, key: Any) -> bool:
    if isinstance(container, dict):
        return key in container
    if isinstance(container, list):
        return isinstance(key, int) and 0 <= key < len(container)
    return False


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _add_output_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-chars",
        type=max_chars_type,
        default=DEFAULT_MAX_CHARS,
        help=f"output cap ({MIN_MAX_CHARS}..{MAX_MAX_CHARS}; default: {DEFAULT_MAX_CHARS})",
    )


def _add_result_limit(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--limit",
        type=limit_type,
        default=DEFAULT_LIMIT,
        help=f"result cap (1..200; default: {DEFAULT_LIMIT})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--spec",
        metavar="PATH",
        help="OpenAPI JSON path (otherwise MIST_OPENAPI_PATH or the user cache)",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    info = subparsers.add_parser(
        "info", help="spec metadata, servers, security schemes"
    )
    _add_output_limit(info)

    find = subparsers.add_parser("find", help="search paths and operation metadata")
    find.add_argument("term")
    _add_result_limit(find)
    _add_output_limit(find)

    show = subparsers.add_parser("show", help="show one operation in detail")
    show.add_argument("method")
    show.add_argument("path")
    _add_output_limit(show)

    schema = subparsers.add_parser("schema", help="dump an expanded component schema")
    schema.add_argument("name")
    schema.add_argument(
        "--property",
        help="return only one exact property (case-insensitive)",
    )
    schema.add_argument("--max-depth", type=depth_type, default=2)
    _add_result_limit(schema)
    _add_output_limit(schema)

    operation = subparsers.add_parser(
        "operation", help="operation plus expanded request/response schemas as JSON"
    )
    operation.add_argument("method")
    operation.add_argument("path")
    operation.add_argument("--max-depth", type=depth_type, default=0)
    _add_output_limit(operation)

    tags = subparsers.add_parser("tags", help="list tags with operation counts")
    _add_result_limit(tags)
    _add_output_limit(tags)

    tag = subparsers.add_parser("tag", help="list operations carrying a tag")
    tag.add_argument("name")
    _add_result_limit(tag)
    _add_output_limit(tag)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        spec = load_spec(_resolve_spec_path(args.spec))
        dispatch = {
            "info": cmd_info,
            "find": cmd_find,
            "show": cmd_show,
            "schema": cmd_schema,
            "operation": cmd_operation,
            "tags": cmd_tags,
            "tag": cmd_tag,
        }
        output = dispatch[args.cmd](spec, args)
    except QueryError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    if output.is_json:
        rendered = _render_json(output.value, args.max_chars, output.truncation_reasons)
    else:
        rendered = _render_text(output.value, args.max_chars)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
