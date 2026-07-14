"""MetaTrader 5 data collector — one of the two Windows-bound edges of the system.

Backfills historical OHLCV into Parquet, publishes live candles to Redis, and
captures instrument specs. Together with the executor, this is the *only* place
allowed to import ``MetaTrader5`` (AGENTS.md §5.4, enforced by
``tests/test_architecture.py``).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
