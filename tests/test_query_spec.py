from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import query_spec


ROOT = Path(__file__).resolve().parents[1]
QUERY_SPEC = ROOT / "scripts" / "query_spec.py"


class QuerySpecCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.spec_path = self.directory / "openapi.json"
        self.spec_path.write_text(json.dumps(self._fixture_spec()), encoding="utf-8")

    @staticmethod
    def _fixture_spec() -> dict:
        huge_properties = {
            f"field_{number:03d}": {
                "type": "string",
                "description": "large schema field " + ("x" * 160),
            }
            for number in range(100)
        }
        return {
            "openapi": "3.1.0",
            "info": {"title": "Mist Fixture API", "version": "1.0"},
            "security": [{"BearerAuth": []}],
            "paths": {
                "/api/v1/widgets": {
                    "parameters": [
                        {
                            "$ref": "#/components/parameters/TenantId",
                            "description": "path-level ref sibling",
                        }
                    ],
                    "get": {
                        "operationId": "listWidgets",
                        "summary": "List widgets",
                        "deprecated": True,
                        "tags": ["Widgets"],
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/Node"}
                                    }
                                },
                            }
                        },
                    },
                    "post": {
                        "operationId": "createWidget",
                        "security": [],
                        "responses": {"204": {"description": "Created"}},
                    },
                }
            },
            "components": {
                "securitySchemes": {"BearerAuth": {"type": "http", "scheme": "bearer"}},
                "parameters": {
                    "TenantId": {
                        "name": "tenant_id",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                },
                "schemas": {
                    "Node": {
                        "type": "object",
                        "deprecated": True,
                        "properties": {
                            "children": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Node"},
                            },
                            "password": {
                                "type": "string",
                                "default": "should-never-be-emitted",
                                "examples": ["also-secret"],
                            },
                            "psk": {
                                "type": "string",
                                "default": "also-must-not-be-emitted",
                            },
                        },
                    },
                    "Huge": {"type": "object", "properties": huge_properties},
                    "A/B~C": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string", "contentEncoding": "base64"},
                    },
                    "EscapedRef": {
                        "$ref": "#/components/schemas/A~1B~0C",
                        "description": "ref sibling survives",
                        "discriminator": {"propertyName": "kind"},
                    },
                },
            },
        }

    def run_cli(
        self,
        *arguments: str,
        spec: Path | None = None,
        include_explicit_spec: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(QUERY_SPEC)]
        if include_explicit_spec:
            command.extend(("--spec", str(spec or self.spec_path)))
        command.extend(arguments)
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            env=env,
        )

    def test_negative_and_out_of_range_numeric_options_fail(self) -> None:
        cases = (
            ("find", "widget", "--limit", "-1"),
            ("schema", "Node", "--max-depth", "-1"),
            ("operation", "GET", "/api/v1/widgets", "--max-chars", "499"),
            ("tags", "--limit", "201"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must be between", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_large_schema_output_is_bounded_and_valid_json(self) -> None:
        result = self.run_cli(
            "schema", "Huge", "--max-depth", "5", "--max-chars", "500"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout), 500)
        document = json.loads(result.stdout)
        self.assertTrue(document["_meta"]["truncated"])
        self.assertIn("output-character-budget", document["_meta"]["reasons"])

    def test_large_schema_is_trimmed_to_property_names_not_wiped(self) -> None:
        result = self.run_cli("schema", "Huge", "--max-depth", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout), 6_000)
        document = json.loads(result.stdout)
        self.assertIn("output-character-budget", document["_meta"]["reasons"])
        properties = document["data"]["schema"]["properties"]
        self.assertEqual(len(properties), 100)
        self.assertEqual(properties["field_099"], "string")

        tight = self.run_cli("schema", "Huge", "--max-chars", "1500")
        self.assertEqual(tight.returncode, 0, tight.stderr)
        self.assertLessEqual(len(tight.stdout), 1_500)
        schema = json.loads(tight.stdout)["data"]["schema"]
        self.assertEqual(schema["type"], "object")
        kept = [name for name in schema["properties"] if name.startswith("field_")]
        self.assertEqual(kept[0], "field_000")
        self.assertEqual(
            len(kept) + schema["properties"]["x-query-omitted"], len(properties)
        )

    def test_render_collapses_nested_schemas_to_labels_before_dropping(
        self,
    ) -> None:
        nested = {
            "allOf": [
                {
                    "type": "object",
                    "x-expanded-from": "#/components/schemas/wlan_airwatch",
                    "properties": {"enabled": {"type": "boolean"}},
                },
                {"description": "Integration settings " + "x" * 200},
            ]
        }
        data = {
            "schema": {
                "type": "object",
                "properties": {
                    "airwatch": nested,
                    "ap_ids": {
                        "type": ["array", "null"],
                        "items": {"type": "string"},
                        "description": "y" * 200,
                    },
                    "ssid": {"type": "string", "description": "z" * 200},
                },
            }
        }
        rendered = query_spec._render_json(data, 400)
        self.assertLessEqual(len(rendered), 400)
        properties = json.loads(rendered)["data"]["schema"]["properties"]
        self.assertEqual(
            properties,
            {
                "airwatch": "wlan_airwatch",
                "ap_ids": "array[string]|null",
                "ssid": "string",
            },
        )

    def test_cycle_terminates_and_secret_defaults_and_examples_are_omitted(
        self,
    ) -> None:
        result = self.run_cli("schema", "Node", "--max-depth", "5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout), 6_000)
        document = json.loads(result.stdout)
        serialized = json.dumps(document)
        self.assertIn("$ref", serialized)
        self.assertNotIn("should-never-be-emitted", serialized)
        self.assertNotIn("also-secret", serialized)
        self.assertNotIn("also-must-not-be-emitted", serialized)

        property_result = self.run_cli(
            "schema", "Node", "--property", "PASSWORD", "--max-depth", "1"
        )
        self.assertEqual(property_result.returncode, 0, property_result.stderr)
        property_document = json.loads(property_result.stdout)["data"]
        self.assertEqual(property_document["property"], "password")
        self.assertFalse(property_document["required"])
        self.assertNotIn("default", property_document["schema"])
        self.assertNotIn("examples", property_document["schema"])

        missing_property = self.run_cli(
            "schema", "Node", "--property", "does_not_exist"
        )
        self.assertEqual(missing_property.returncode, 2)
        self.assertIn("has no property", missing_property.stderr)

    def test_deprecated_effective_security_and_path_parameters_are_visible(
        self,
    ) -> None:
        result = self.run_cli("operation", "GET", "/api/v1/widgets")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["data"]
        self.assertTrue(data["deprecated"])
        self.assertEqual(data["security"], [{"BearerAuth": []}])
        self.assertEqual(data["parameters"][0]["name"], "tenant_id")
        self.assertEqual(data["parameters"][0]["description"], "path-level ref sibling")

        public_result = self.run_cli("operation", "POST", "/api/v1/widgets")
        self.assertEqual(json.loads(public_result.stdout)["data"]["security"], [])

        show_result = self.run_cli("show", "GET", "/api/v1/widgets")
        self.assertIn("deprecated: true", show_result.stdout)
        self.assertIn('security: [{"BearerAuth": []}]', show_result.stdout)
        self.assertIn("tenant_id", show_result.stdout)

        find_result = self.run_cli("find", "widgets")
        self.assertIn("[DEPRECATED]", find_result.stdout)

    def test_show_names_schemas_inside_composition_and_array_items(self) -> None:
        spec = {
            "openapi": "3.1.0",
            "components": {"schemas": {"wlan": {}, "site": {}, "a": {}, "b": {}}},
            "paths": {
                "/wlans/{id}": {
                    "put": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [
                                            {"$ref": "#/components/schemas/wlan"},
                                            {"description": "Request Body"},
                                        ]
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {
                                "description": "OK",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {
                                                "$ref": "#/components/schemas/site"
                                            },
                                        }
                                    }
                                },
                            },
                            "201": {
                                "description": "Either",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "oneOf": [
                                                {"$ref": "#/components/schemas/a"},
                                                {"$ref": "#/components/schemas/b"},
                                            ]
                                        }
                                    }
                                },
                            },
                            "204": {"description": "Empty"},
                        },
                    }
                }
            },
        }
        args = argparse.Namespace(method="put", path="/wlans/{id}")
        text = query_spec.cmd_show(spec, args).value

        self.assertIn("application/json -> schema: allOf[wlan]", text)
        self.assertIn("200: OK  -> array[site]", text)
        self.assertIn("201: Either  -> oneOf[a, b]", text)
        self.assertIn("  204: Empty\n", text + "\n")

    def test_pointer_escaping_ref_siblings_and_schema_semantics_survive(self) -> None:
        result = self.run_cli("schema", "EscapedRef", "--max-depth", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        schema = json.loads(result.stdout)["data"]["schema"]
        self.assertEqual(schema["x-expanded-from"], "#/components/schemas/A~1B~0C")
        self.assertEqual(schema["description"], "ref sibling survives")
        self.assertEqual(schema["discriminator"], {"propertyName": "kind"})
        self.assertTrue(schema["uniqueItems"])
        self.assertEqual(schema["items"]["contentEncoding"], "base64")
        self.assertNotIn("$schema", schema)

    def test_missing_and_malformed_specs_fail_without_tracebacks(self) -> None:
        missing = self.run_cli("info", spec=self.directory / "missing.json")
        self.assertEqual(missing.returncode, 2)
        self.assertIn("OpenAPI spec not found", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)

        malformed_path = self.directory / "malformed.json"
        malformed_path.write_text('{"openapi":', encoding="utf-8")
        malformed = self.run_cli("info", spec=malformed_path)
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("Malformed OpenAPI JSON", malformed.stderr)
        self.assertIn("line 1", malformed.stderr)
        self.assertNotIn("Traceback", malformed.stderr)

        unrelated_path = self.directory / "unrelated.json"
        unrelated = self._fixture_spec()
        unrelated["info"]["title"] = "Unrelated API"
        unrelated_path.write_text(json.dumps(unrelated), encoding="utf-8")
        wrong_api = self.run_cli("info", spec=unrelated_path)
        self.assertEqual(wrong_api.returncode, 2)
        self.assertIn("not a valid Mist document", wrong_api.stderr)
        self.assertNotIn("Traceback", wrong_api.stderr)

    def test_oversized_explicit_spec_is_rejected_before_json_loading(self) -> None:
        oversized = self.directory / "oversized.json"
        oversized.write_bytes(b"x" * 17)
        with mock.patch.object(query_spec, "MAX_SPEC_BYTES", 16):
            with self.assertRaisesRegex(query_spec.QueryError, "size limit"):
                query_spec.load_spec(oversized)

    def test_environment_variable_is_used_when_spec_is_not_explicit(self) -> None:
        environment = os.environ.copy()
        environment["MIST_OPENAPI_PATH"] = str(self.spec_path)
        result = self.run_cli("info", include_explicit_spec=False, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Fixture API", result.stdout)


if __name__ == "__main__":
    unittest.main()
