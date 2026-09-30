from __future__ import annotations

import argparse
import unittest

from scripts import query_spec


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _fixture_spec() -> dict:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Composition Fixture", "version": "1.0"},
        "paths": {},
        "components": {
            "schemas": {
                "deviceprofile": {
                    "oneOf": [
                        _ref("deviceprofile_ap"),
                        _ref("deviceprofile_gateway"),
                        _ref("deviceprofile_switch"),
                    ],
                    "discriminator": {
                        "propertyName": "type",
                        "mapping": {
                            "ap": "#/components/schemas/deviceprofile_ap",
                            "gateway": "#/components/schemas/deviceprofile_gateway",
                            "switch": "#/components/schemas/deviceprofile_switch",
                        },
                    },
                },
                "deviceprofile_ap": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {"type": "string", "enum": ["ap"]},
                        "name": {"type": ["string", "null"]},
                    },
                },
                "deviceprofile_gateway": {
                    "type": "object",
                    "required": ["type", "name"],
                    "properties": {
                        "type": {"type": "string", "enum": ["gateway"]},
                        "name": {"type": "string"},
                        "networks": {"type": "array", "items": _ref("network")},
                    },
                },
                "deviceprofile_switch": {
                    "type": "object",
                    "required": ["type", "name"],
                    "properties": {
                        "type": {"type": "string", "enum": ["switch"]},
                        "name": {"type": "string"},
                        "networks": {
                            "type": "object",
                            "additionalProperties": _ref("switch_network"),
                        },
                    },
                },
                "network": {
                    "type": "object",
                    "properties": {"subnet": {"type": "string"}},
                },
                "switch_network": {
                    "type": "object",
                    "properties": {"vlan_id": {"type": "integer"}},
                },
                "stats_device": {
                    "oneOf": [
                        _ref("stats_ap"),
                        _ref("stats_switch"),
                        _ref("stats_gateway"),
                    ],
                },
                "stats_ap": {
                    "type": "object",
                    "properties": {"mac": {"type": "string"}},
                },
                "stats_switch": {
                    "type": "object",
                    "properties": {
                        "mac": {"type": "string"},
                        "hostname": {"type": "string"},
                    },
                },
                "stats_gateway": {
                    "type": "object",
                    "properties": {"mac": {"type": "string"}},
                },
                "tightened": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {
                                "port": {"type": ["integer", "null"], "minimum": 0},
                            },
                        },
                        {
                            "type": "object",
                            "required": ["port"],
                            "properties": {
                                "port": {"type": "integer", "minimum": 1},
                            },
                        },
                    ],
                },
                "cased": {
                    "type": "object",
                    "properties": {
                        "Name": {"type": "string"},
                        "name": {"type": "string"},
                    },
                },
            }
        },
    }


def _schema_property(name: str, property_name: str) -> dict:
    args = argparse.Namespace(
        name=name, property=property_name, max_depth=3, limit=20
    )
    output = query_spec.cmd_schema(_fixture_spec(), args)
    return output.value


class SchemaPropertyCompositionTests(unittest.TestCase):
    def test_oneof_required_and_type_are_reported_per_variant(self) -> None:
        value = _schema_property("deviceprofile", "name")

        self.assertEqual(value["component"], "deviceprofile")
        self.assertEqual(value["property"], "name")
        self.assertNotIn("required", value)
        self.assertNotIn("schema", value)
        self.assertEqual(value["composition"], "oneOf")
        self.assertEqual(value["discriminator"], "type")
        variants = value["variants"]
        self.assertEqual(set(variants), {"ap", "gateway", "switch"})
        self.assertFalse(variants["ap"]["required"])
        self.assertEqual(variants["ap"]["schema"]["type"], ["string", "null"])
        for label in ("gateway", "switch"):
            self.assertTrue(variants[label]["required"])
            self.assertEqual(variants[label]["schema"]["type"], "string")
        self.assertEqual(value["absentFrom"], [])

    def test_oneof_variant_specific_shapes_are_all_reported(self) -> None:
        value = _schema_property("deviceprofile", "networks")

        self.assertNotIn("schema", value)
        variants = value["variants"]
        self.assertEqual(set(variants), {"gateway", "switch"})
        self.assertEqual(variants["gateway"]["schema"]["type"], "array")
        self.assertEqual(variants["switch"]["schema"]["type"], "object")
        self.assertEqual(value["absentFrom"], ["ap"])

    def test_oneof_field_on_one_variant_names_that_variant(self) -> None:
        value = _schema_property("stats_device", "hostname")

        self.assertNotIn("schema", value)
        self.assertNotIn("required", value)
        self.assertEqual(list(value["variants"]), ["stats_switch"])
        self.assertEqual(value["absentFrom"], ["stats_ap", "stats_gateway"])

    def test_oneof_field_identical_in_every_variant_stays_flat(self) -> None:
        value = _schema_property("stats_device", "mac")

        self.assertEqual(value["schema"]["type"], "string")
        self.assertFalse(value["required"])
        self.assertNotIn("variants", value)

    def test_allof_merges_later_type_and_required_constraints(self) -> None:
        value = _schema_property("tightened", "port")

        self.assertTrue(value["required"])
        self.assertEqual(value["schema"]["type"], "integer")
        self.assertEqual(value["schema"]["minimum"], 1)
        self.assertNotIn("variants", value)

    def test_letter_case_ambiguity_is_still_an_error(self) -> None:
        with self.assertRaisesRegex(query_spec.QueryError, "ambiguous by letter case"):
            _schema_property("cased", "NAME")


if __name__ == "__main__":
    unittest.main()
