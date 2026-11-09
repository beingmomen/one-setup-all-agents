"""Tests for _core.jsonc_edit: comment-preserving JSONC object editing."""

import json
import unittest

import _bootstrap  # noqa: F401
from one_setup_all_agents._core import jsonc, jsonc_edit


class JsoncEditTests(unittest.TestCase):
    DOC = (
        '{\n  "a": 1,\n  "mcp": {\n    "servers": {\n      // first\n      "one": {\n        "x": 1\n      }, // tail\n'
        '      /* second */\n      "two": { "x": 2 },\n      "three": { "x": 3 }\n    }\n  }\n}\n'
    )

    def servers(self, text):
        return jsonc.loads(text)["mcp"]["servers"]

    def test_remove_first_middle_last(self):
        for key, left in (
            ("one", ["two", "three"]),
            ("two", ["one", "three"]),
            ("three", ["one", "two"]),
        ):
            out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=[key])
            self.assertEqual(list(self.servers(out)), left)
            self.assertIn('"a": 1', out)
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["one"])
        self.assertNotIn("// first", out)
        self.assertNotIn("// tail", out)  # the member's own comments go with it
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["two"])
        self.assertNotIn("/* second */", out)
        self.assertIn("// tail", out)  # the previous member keeps its trailing comment

    def test_removing_last_member_keeps_strict_json(self):
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["three"])
        json.loads(
            jsonc.strip_jsonc(out).replace(",\n    }", "\n    }")
        )  # parses even without the tolerant reader
        self.assertNotIn("}, }", out)

    def test_replace_keeps_comments_above(self):
        out = jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], replaces={"one": {"y": [1, 2]}})
        self.assertIn("// first", out)
        self.assertEqual(self.servers(out)["one"], {"y": [1, 2]})
        self.assertEqual(self.servers(out)["two"], {"x": 2})

    def test_add_at_end_adds_missing_comma_and_comment(self):
        out = jsonc_edit.edit_object(
            self.DOC, ["mcp", "servers"], adds=[("four", {"x": 4}, "added by test")]
        )
        self.assertEqual(list(self.servers(out)), ["one", "two", "three", "four"])
        self.assertIn("// added by test", out)
        self.assertTrue(out.startswith(self.DOC[: self.DOC.index('"three"')]))

    def test_add_to_empty_object_and_unchanged_rest(self):
        doc = '{\n  // keep\n  "mcp": {\n    "servers": {}\n  },\n  "z": "//not a comment"\n}\n'
        out = jsonc_edit.edit_object(doc, ["mcp", "servers"], adds=[("n", {"a": 1}, None)])
        self.assertEqual(jsonc.loads(out)["mcp"]["servers"], {"n": {"a": 1}})
        self.assertIn("// keep", out)
        self.assertIn('"//not a comment"', out)

    def test_braces_and_comment_markers_inside_strings(self):
        doc = '{"mcp": {"servers": {"a": {"u": "x}//y"}, "b": {"v": "{"}}}}'
        out = jsonc_edit.edit_object(doc, ["mcp", "servers"], removes=["a"])
        self.assertEqual(jsonc.loads(out)["mcp"]["servers"], {"b": {"v": "{"}})

    def test_nested_edit_keeps_inner_comments_and_other_keys(self):
        doc = (
            '{\n "mcp": {"servers": {\n  "a": {\n   "type": "local",\n   // explains the command\n'
            '   "command": ["x", "y"],\n   "extra": 1\n  }\n }}}\n'
        )
        out = jsonc_edit.edit_object(
            doc,
            ["mcp", "servers", "a"],
            removes=["extra"],
            replaces={"command": ["x", "z"]},
            adds=[("env", {"K": "v"}, None)],
        )
        self.assertIn("// explains the command", out)
        self.assertEqual(
            jsonc.loads(out)["mcp"]["servers"]["a"],
            {"type": "local", "command": ["x", "z"], "env": {"K": "v"}},
        )

    def test_missing_path_or_member(self):
        with self.assertRaises(jsonc_edit.EditError):
            jsonc_edit.edit_object(self.DOC, ["nope"])
        with self.assertRaises(jsonc_edit.EditError):
            jsonc_edit.edit_object(self.DOC, ["mcp", "servers"], removes=["zzz"])


if __name__ == "__main__":
    unittest.main()
