"""Tests for _core.jsonc: comments, trailing commas, CRLF, and BOM handling."""

import json
import unittest

import _bootstrap  # noqa: F401
from one_setup_all_agents._core import jsonc


class JsoncTests(unittest.TestCase):
    def test_comments_and_trailing_commas(self):
        text = '{\n // line\n "a": 1, /* block */ "b": [1, 2,],\n "c": {"d": 3,},\n}\n'
        self.assertEqual(jsonc.loads(text), {"a": 1, "b": [1, 2], "c": {"d": 3}})

    def test_comment_markers_inside_strings_are_kept(self):
        text = '{"u": "https://x.test/a//b", "s": "/* not a comment */", "e": "q\\"//still string"}'
        self.assertEqual(jsonc.loads(text), json.loads(text))

    def test_crlf_and_bom(self):
        text = '\ufeff{\r\n  // c\r\n  "a": 1,\r\n}\r\n'
        self.assertEqual(jsonc.loads(text), {"a": 1})

    def test_comma_before_closer_inside_string_is_kept(self):
        self.assertEqual(jsonc.loads('{"a": "x,}"}'), {"a": "x,}"})

    def test_unterminated_block_comment(self):
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": 1 /* oops')

    def test_error_line_number_survives_block_comments(self):
        try:
            jsonc.loads('{\n/* a\nb\nc */\n"x": nope\n}')
        except json.JSONDecodeError as e:
            self.assertEqual(e.lineno, 5)
        else:
            self.fail("expected a JSON error")


if __name__ == "__main__":
    unittest.main()
