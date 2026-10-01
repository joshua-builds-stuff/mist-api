from __future__ import annotations

import argparse
import unittest

from scripts import query_spec


DOT11_BAND = [
    "24",
    "5",
    "5-dedicated",
    "5-selectable",
    "6",
    "6-dedicated",
    "6-selectable",
]


def _spec() -> dict:
    return {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "dot11_band": {"type": "string", "enum": DOT11_BAND},
                "oauth_app_name": {
                    "type": "string",
                    "enum": ["teams", "zoom", "intune"],
                },
                "site_id": {"type": "string", "format": "uuid"},
                "a": {"type": "string", "enum": ["x"]},
                "b": {"type": "integer", "const": 7},
            }
        },
        "paths": {
            "/sites/{site_id}/bands/{band}": {
                "get": {
                    "parameters": [
                        {
                            "name": "band",
                            "in": "path",
                            "required": True,
                            "description": "802.11 Band",
                            "schema": {
                                "allOf": [
                                    {"$ref": "#/components/schemas/dot11_band"},
                                    {"description": "802.11 Band"},
                                ]
                            },
                        },
                        {
                            "name": "site_id",
                            "in": "path",
                            "required": True,
                            "schema": {"$ref": "#/components/schemas/site_id"},
                        },
                        {
                            "name": "app_name",
                            "in": "query",
                            "description": "OAuth app name",
                            "schema": {
                                "allOf": [
                                    {"$ref": "#/components/schemas/oauth_app_name"},
                                    {"description": "OAuth app name"},
                                ]
                            },
                        },
                        {
                            "name": "either",
                            "in": "query",
                            "schema": {
                                "oneOf": [
                                    {"$ref": "#/components/schemas/a"},
                                    {"$ref": "#/components/schemas/b"},
                                ]
                            },
                        },
                        {
                            "name": "any",
                            "in": "query",
                            "schema": {
                                "anyOf": [
                                    {"$ref": "#/components/schemas/a"},
                                    {"$ref": "#/components/schemas/dot11_band"},
                                ]
                            },
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {"type": "integer"},
                        },
                    ],
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }


class ShowComposedParameterSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        args = argparse.Namespace(method="get", path="/sites/{site_id}/bands/{band}")
        self.text = query_spec.cmd_show(_spec(), args).value

    def _line(self, name: str) -> str:
        for line in self.text.splitlines():
            if line.startswith(f"  - {name} ("):
                return line
        self.fail(f"no parameter line for {name}:\n{self.text}")

    def test_allof_ref_path_parameter_names_component_type_and_enum(self) -> None:
        line = self._line("band")
        self.assertNotIn(", ?, ", line)
        self.assertIn("dot11_band", line)
        self.assertIn("string", line)
        self.assertIn("required", line)
        for value in ("24", "5", "6", "5-dedicated", "6-selectable"):
            self.assertIn(value, line)

    def test_several_refs_are_listed(self) -> None:
        either = self._line("either")
        self.assertNotIn(", ?, ", either)
        self.assertIn("oneOf[", either)
        self.assertIn("a string enum[x]", either)
        self.assertIn("b integer const=7", either)

        any_line = self._line("any")
        self.assertNotIn(", ?, ", any_line)
        self.assertIn("anyOf[", any_line)
        self.assertIn("a ", any_line)
        self.assertIn("dot11_band", any_line)

    def test_top_level_ref_still_names_component(self) -> None:
        line = self._line("site_id")
        self.assertIn("(path, site_id string, required)", line)

    def test_plain_type_still_printed(self) -> None:
        self.assertIn("  - limit (query, integer, optional)", self.text)

    def test_query_enum_only_on_component_is_surfaced(self) -> None:
        line = self._line("app_name")
        self.assertIn("oauth_app_name", line)
        self.assertIn("enum[teams, zoom, intune]", line)
        self.assertIn(": OAuth app name", line)

    def test_request_body_and_response_labels_unchanged(self) -> None:
        self.assertIn("  200: OK", self.text)
        self.assertNotIn("requestBody:", self.text)


if __name__ == "__main__":
    unittest.main()
