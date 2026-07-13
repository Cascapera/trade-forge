"""Smoke test: the schema package is importable from the installed workspace."""

import tradeforge_schema


def test_schema_exposes_a_version() -> None:
    assert tradeforge_schema.__version__ == "0.1.0"
