"""Persistence for TradeForge: the SQLAlchemy models and the Alembic migrations.

Shared by the API, the collector and the executor (ADR-0009). Deliberately *not*
imported by `packages/engine`: the engine is pure — candles in, orders out — and a
core that can reach a database is a core whose results depend on what is in it.

What is stored here is metadata and results. The candles themselves live in Parquet
(ADR-05); `datasets` is the catalogue that says which ones exist.
"""

from tradeforge_db.base import MONEY, PRICE, RATIO, VOLUME, Base
from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import alembic_config, downgrade, heads, upgrade
from tradeforge_db.models import (
    AssetClass,
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Dataset,
    Direction,
    ExitReason,
    Instrument,
    Strategy,
    Trade,
)
from tradeforge_db.seeds import INSTRUMENT_SEEDS, InstrumentSeed, seed_instruments
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope

__all__ = [
    "INSTRUMENT_SEEDS",
    "MONEY",
    "PRICE",
    "RATIO",
    "VOLUME",
    "AssetClass",
    "Backtest",
    "BacktestMetrics",
    "BacktestStatus",
    "Base",
    "Dataset",
    "Direction",
    "ExitReason",
    "Instrument",
    "InstrumentSeed",
    "PostgresSettings",
    "Strategy",
    "Trade",
    "__version__",
    "alembic_config",
    "create_db_engine",
    "create_session_factory",
    "downgrade",
    "heads",
    "seed_instruments",
    "session_scope",
    "upgrade",
]

__version__ = "0.1.0"
