"""MetaTrader 5 data collector — one of the two Windows-bound edges of the system.

Backfills historical OHLCV into Parquet, catalogues it in Postgres, and reports the
gaps. Together with the executor, this is the *only* place allowed to import
``MetaTrader5`` (AGENTS.md §5.4, enforced by ``tests/test_architecture.py``).

Note what is **not** imported below: ``mt5_source``. The library it needs has Windows
wheels only, so importing it here would make the whole package unimportable on Linux —
and with it, the tests. The real source is loaded on demand, at the moment someone asks
for real data; everything else in this package runs anywhere.
"""

from tradeforge_collector.backfill import BackfillReport, backfill
from tradeforge_collector.gaps import Gap, anomalies, find_gaps, format_report
from tradeforge_collector.source import Candle, MarketDataSource
from tradeforge_collector.storage import OHLCV_SCHEMA, read_candles, write_candles
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_collector.timeframes import TIMEFRAME_STEP, step

__all__ = [
    "OHLCV_SCHEMA",
    "TIMEFRAME_STEP",
    "BackfillReport",
    "Candle",
    "Gap",
    "MarketDataSource",
    "SyntheticSource",
    "__version__",
    "anomalies",
    "backfill",
    "find_gaps",
    "format_report",
    "read_candles",
    "step",
    "write_candles",
]

__version__ = "0.1.0"
