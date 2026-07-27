"""Make the repo's CI scripts importable by the tests that cover them.

`scripts/` is not a package and must not become one: the files there are run by the workflow as
plain scripts, with no install step and no dependency on the workspace venv being synced. Putting
the directory on `sys.path` here is what lets `tests/` import them by name anyway — mypy is told
the same thing through `mypy_path` in pyproject.toml, so the scripts stay under `--strict`.
"""

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
