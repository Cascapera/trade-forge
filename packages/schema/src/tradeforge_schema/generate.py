"""Emit the JSON Schema from the Pydantic models.

The `.json` file is a build artefact that happens to be committed — like a lockfile.
It is committed because the frontend and (phase 3) an LLM need to read it without a
Python runtime, and it is checked in CI because a generated file that nobody verifies
is a generated file somebody eventually hand-edits.
"""

import json
from pathlib import Path
from typing import Any

from tradeforge_schema.models import Strategy

SCHEMA_PATH = Path(__file__).parent / "strategy.schema.json"

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def build_schema() -> dict[str, Any]:
    """The strategy JSON Schema, as a dict."""
    schema: dict[str, Any] = Strategy.model_json_schema(by_alias=True, mode="validation")
    schema["$schema"] = JSON_SCHEMA_DIALECT
    return schema


def render_schema() -> str:
    """The exact bytes that belong on disk. Deterministic: same models, same file."""
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Write the schema to disk. `uv run tradeforge-schema-gen`.

    ⚠️ **`newline=""`, and it is not cosmetic.** `write_text` opens in text mode, which on Windows
    turns every `\n` into `\r\n` — so the committed file grew a thousand carriage returns every
    time this ran, while `render_schema()` (which the drift test compares against) keeps the `\n`
    it built. The file the repository stores is normalised to LF by `.gitattributes`, so the diff
    looked clean and the working tree did not, and the loop only showed up as `git ls-files --eol`
    reporting `w/crlf` after every test run — `test_semantic.py` calls this function.

    Writing bytes with an explicit empty newline translation makes the generator produce the same
    file on every platform, which is the whole promise of a generated artefact.
    """
    with SCHEMA_PATH.open("w", encoding="utf-8", newline="") as handle:
        handle.write(render_schema())
    print(f"wrote {SCHEMA_PATH}")  # noqa: T201 — a generator's job is to say what it wrote
    return 0
