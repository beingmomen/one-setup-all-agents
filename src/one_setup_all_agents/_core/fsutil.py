"""Small file-system helpers that must behave the same on Python 3.11 to 3.13 and on every OS."""

import os
import stat


def is_junction(path):
    """True for a Windows directory junction. Always False elsewhere.

    `os.path.isjunction` exists only from Python 3.12; the minimum supported version is 3.11 (ADR 0004), so older
    interpreters read the reparse tag themselves."""
    isj = getattr(os.path, "isjunction", None)
    if isj is not None:
        return bool(isj(str(path)))
    if os.name != "nt":
        return False
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == stat.IO_REPARSE_TAG_MOUNT_POINT
