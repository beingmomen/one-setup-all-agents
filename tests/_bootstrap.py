"""Put `src/` on sys.path so the tests run from a clean checkout with no install.

Every test module starts with `import _bootstrap`. `unittest discover -s tests`
makes `tests/` importable as the top level, so this module is found by name.
If the package is installed (CI uses `pip install -e .`), the installed copy and
`src/` are the same code, so nothing changes.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
