"""The committed JSON Schema must be exactly what the models produce.

A generated file that nobody verifies is a generated file somebody eventually
hand-edits — and then the contract the frontend reads and the contract the backend
enforces quietly stop being the same contract. Same discipline as a lockfile.
"""

from tradeforge_schema.generate import SCHEMA_PATH, render_schema


def test_committed_schema_matches_the_models() -> None:
    on_disk = SCHEMA_PATH.read_text(encoding="utf-8")

    assert on_disk == render_schema(), (
        "strategy.schema.json is stale. Regenerate it with `uv run tradeforge-schema-gen`."
    )


def test_rendering_is_deterministic() -> None:
    """Same models, same bytes — otherwise the drift test would flap forever."""
    assert render_schema() == render_schema()
