"""Event-driven strategy engine — the core of TradeForge.

The engine is the single place where strategy logic lives. It runs unchanged against a
``BacktestBroker``, a ``PaperBroker`` or a live ``MT5Broker``, which is what keeps
backtest results and live results describing the same system.

It has **no dependencies**. Not "few" — none. It cannot reach a database, a broker, a
file or a clock; everything it knows arrives as an argument. That is not asceticism, it
is the mechanism behind determinism: a function of its inputs has no other way to
produce a different answer tomorrow.

Three invariants hold here and are enforced by tests (AGENTS.md §5):

* **No lookahead** — a decision made on the close of candle N is filled at the open of
  candle N+1. The engine checks every fill against the instant its order was decided and
  raises ``LookaheadError`` rather than trust the broker that produced it.
* **Determinism** — the same input always produces the same output.
* **Broker agnosticism** — the engine never learns where an order is executed.
"""

from tradeforge_engine.domain import (
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Context,
    EquityPoint,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Side,
    Signal,
    SignalKind,
    Volume,
)
from tradeforge_engine.errors import EngineError, LookaheadError
from tradeforge_engine.loop import RunResult, run
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.protocols import (
    Broker,
    Condition,
    CostModel,
    Indicator,
    RiskManager,
    Strategy,
)

__all__ = [
    "AccountState",
    "AssetClass",
    "Broker",
    "Candle",
    "ClosedTrade",
    "Condition",
    "Context",
    "CostModel",
    "EngineError",
    "EquityPoint",
    "Fill",
    "Indicator",
    "InstrumentSpec",
    "LookaheadError",
    "Money",
    "OrderRequest",
    "OrderResult",
    "Portfolio",
    "Position",
    "RiskManager",
    "RunResult",
    "Side",
    "Signal",
    "SignalKind",
    "Strategy",
    "Volume",
    "__version__",
    "run",
]

__version__ = "0.1.0"
