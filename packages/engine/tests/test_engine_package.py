"""Smoke test: the engine is importable from the installed workspace package.

This is deliberately not ``assert True``. It fails if the uv workspace wiring or
the src-layout packaging is broken, which is exactly what PR-001 must guarantee.
"""

import tradeforge_engine


def test_engine_exposes_a_version() -> None:
    assert tradeforge_engine.__version__ == "0.1.0"
