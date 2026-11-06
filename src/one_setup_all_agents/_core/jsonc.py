"""Minimal JSONC reader: strips // and /* */ comments and trailing commas (string-aware), then json.loads.

Error messages carry line/column only, never file content.
"""

import json


def strip_jsonc(text):
    """Return `text` with comments and trailing commas removed. Strings are left untouched."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':  # copy a whole string literal, honouring escapes
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                raise ValueError("unterminated /* comment")
            # keep newlines so that json error line numbers still point at the original file
            out.append("\n" * text.count("\n", i, end))
            i = end + 2
        else:
            out.append(c)
            i += 1
    return _drop_trailing_commas("".join(out))


def _drop_trailing_commas(text):
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    break
                j += 1
            out.append(text[i : j + 1])
            i = j + 1
        elif c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1  # drop the comma, keep the whitespace that follows
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads(text):
    if text.startswith("\ufeff"):
        text = text[1:]
    return json.loads(strip_jsonc(text))
