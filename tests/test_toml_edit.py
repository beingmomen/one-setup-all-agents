"""Tests for _core.toml_edit: section and block-preserving TOML editing."""

import tomllib
import unittest

import _bootstrap  # noqa: F401
from one_setup_all_agents._core import toml_edit


class TomlEditTests(unittest.TestCase):
    DOC = (
        'model = "x"\nlist = [\n  ["a"]\n]\n\n# about a\n[mcp_servers.a]\ncommand = "c"\nenv_vars = ["X"]\n\n'
        '[mcp_servers.a.env]\nK = "v"\n\n[mcp_servers.keep]\nurl = "k"\n\n[projects.\'C:\\x\']\ntrust = "t"\n'
    )

    def parse(self, text):
        return tomllib.loads(text)

    def test_blocks_round_trip_exactly(self):
        self.assertEqual(toml_edit.join_blocks(toml_edit.split_blocks(self.DOC)), self.DOC)

    def test_remove_drops_subtables_and_leading_comment_only(self):
        out = toml_edit.edit(self.DOC, ["a"], [])
        d = self.parse(out)
        self.assertEqual(list(d["mcp_servers"]), ["keep"])
        self.assertNotIn("about a", out)
        self.assertIn("[projects.'C:\\x']", out)
        self.assertEqual(d["list"], [["a"]])

    def test_upsert_moves_into_region_and_is_stable(self):
        entry = {
            "command": "z",
            "args": ["1", "C:\\p q"],
            "startup_timeout_sec": 120,
            "env": {"A": "b\\c"},
        }
        once = toml_edit.edit(self.DOC, [], [("a", entry, "why")])
        d = self.parse(once)
        self.assertEqual(d["mcp_servers"]["a"], entry)
        self.assertEqual(d["mcp_servers"]["keep"], {"url": "k"})
        self.assertEqual(once.count(toml_edit.BEGIN), 1)
        self.assertIn("# why", once)
        twice = toml_edit.edit(once, [], [("a", entry, "why")])
        self.assertEqual(twice, once)  # same input, same output
        other = toml_edit.edit(once, [], [("b", {"url": "u"}, None)])
        self.assertEqual(
            self.parse(other)["mcp_servers"]["a"], entry
        )  # the region keeps what it already had
        self.assertEqual(self.parse(other)["mcp_servers"]["b"], {"url": "u"})

    def test_unclosed_region_is_refused(self):
        with self.assertRaises(toml_edit.TomlEditError):
            toml_edit.edit(
                '# >>> one-setup-all-agents >>>\n[mcp_servers.a]\nurl = "u"\n',
                [],
                [("a", {"url": "v"}, None)],
            )

    def test_bare_mcp_servers_table_is_refused(self):
        with self.assertRaises(toml_edit.TomlEditError):
            toml_edit.edit('[mcp_servers]\na = { url = "u" }\n', [], [("a", {"url": "v"}, None)])


if __name__ == "__main__":
    unittest.main()
