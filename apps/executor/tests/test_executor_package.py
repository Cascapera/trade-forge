"""Smoke test: the executor package imports on every platform (see collector)."""

import tradeforge_executor


def test_executor_exposes_a_version() -> None:
    assert tradeforge_executor.__version__ == "0.1.0"
