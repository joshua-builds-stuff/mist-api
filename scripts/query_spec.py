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
            ref = _ref_name(schema.get("$ref", ""))
            lines.append(f"  {content_type} -> schema: {ref or _schema_label(schema)}")

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
                        schema_ref = _ref_name(media["schema"].get("$ref", ""))
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

    budget = _expansion_budget(args.max_chars)
    if args.property:
        located = _find_schema_property(spec, schemas[name], args.property)
        if located is None:
            raise QueryError(f"Schema '{name}' has no property named '{args.property}'")
        property_name, property_schema, required = located
        document = _expand_schema(
            spec,
            property_schema,
            args.max_depth,
            budget=budget,
            context_name=property_name,
        )
        value = {
            "component": name,
            "property": property_name,
            "required": required,
            "schema": document,
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
    budget = _expansion_budget(args.max_chars)
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


def _find_schema_property(
    spec: dict[str, Any],
    schema: Any,
    requested_name: str,
    seen_refs: frozenset[str] = frozenset(),
) -> tuple[str, dict[str, Any], bool] | None:
    """Find one exact property through local refs and composition keywords."""

    if not isinstance(schema, dict):
        return None

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
            return (
                name,
                property_schema,
                isinstance(required, list) and name in required,
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
            return located

    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            located = _find_schema_property(
                spec,
                branch,
                requested_name,
                seen_refs,
            )
            if located is not None:
                return located
    return None


def _expansion_budget(max_chars: int) -> ExpansionBudget:
    return ExpansionBudget(
        max_nodes=max(32, min(2_500, max_chars // 12)),
        max_properties=max(16, min(1_200, max_chars // 20)),
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

    while len(render()) + 1 > max_chars:
        candidates = _prune_candidates(envelope["data"])
        if not candidates:
            envelope["data"] = {"x-query-truncated": "output-character-budget"}
            break
        _saving, path, replacement = max(candidates, key=lambda item: item[0])
        _replace_at_path(envelope["data"], path, replacement)

    rendered = render()
    if len(rendered) + 1 > max_chars:
        # The minimum allowed max_chars is large enough for this final envelope.
        envelope["data"] = None
        rendered = render()
    return rendered + "\n"


def _prune_candidates(value: Any) -> list[tuple[int, tuple[Any, ...], Any]]:
    candidates: list[tuple[int, tuple[Any, ...], Any]] = []

    def visit(item: Any, path: tuple[Any, ...]) -> None:
        if isinstance(item, str) and len(item) > 20:
            replacement = "… [truncated]"
            saving = _json_size(item) - _json_size(replacement)
            if saving > 0:
                candidates.append((saving, path, replacement))
        elif isinstance(item, dict):
            if path and item:
                replacement = {"x-query-truncated": "output-character-budget"}
                saving = _json_size(item) - _json_size(replacement)
                if saving > 0:
                    candidates.append((saving, path, replacement))
            for key, child in item.items():
                visit(child, (*path, key))
        elif isinstance(item, list):
            if path and item:
                replacement = [{"x-query-truncated": "output-character-budget"}]
                saving = _json_size(item) - _json_size(replacement)
                if saving > 0:
                    candidates.append((saving, path, replacement))
            for index, child in enumerate(item):
                visit(child, (*path, index))

    visit(value, ())
    return candidates


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _replace_at_path(root: Any, path: tuple[Any, ...], replacement: Any) -> None:
    if not path:
        return
    parent = root
    for part in path[:-1]:
        parent = parent[part]
    parent[path[-1]] = replacement


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
