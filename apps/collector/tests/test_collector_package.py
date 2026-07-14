"""Smoke test: the collector package imports on every platform.

Note what is *not* here: an import of ``MetaTrader5``. The collector's own module
tree must stay importable on Linux CI, where no MT5 wheel exists — the library is
loaded behind the broker edge and mocked in tests (see sdd.md §10.4).
"""

import tradeforge_collector


def test_collector_exposes_a_version() -> None:
    assert tradeforge_collector.__version__ == "0.1.0"
