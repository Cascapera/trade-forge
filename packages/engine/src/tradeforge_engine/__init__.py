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

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import (
    BarSpreadCostModel,
    CombinedCostModel,
    CommissionCostModel,
    NoCostModel,
    SpreadCostModel,
)
from tradeforge_engine.domain import (
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Context,
    EquityPoint,
    EvalContext,
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
from tradeforge_engine.indicators import EMA, SMA, build_indicator
from tradeforge_engine.loop import BarOutcome, RunResult, iter_run, run
from tradeforge_engine.metrics import BacktestMetrics, compute_metrics
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.protocols import (
    Broker,
    Condition,
    CostModel,
    Indicator,
    RiskManager,
    Strategy,
)
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.strategy import CompiledStrategy, compile_strategy
from tradeforge_engine.swap import SwapRates
from tradeforge_engine.warmup import HandOver, hand_over, unwarmed_indicators

__all__ = [
    "EMA",
    "SMA",
    "AccountState",
    "AssetClass",
    "BacktestBroker",
    "BacktestMetrics",
    "BarOutcome",
    "BarSpreadCostModel",
    "Broker",
    "Candle",
    "ClosedTrade",
    "CombinedCostModel",
    "CommissionCostModel",
    "CompiledStrategy",
    "Condition",
    "Context",
    "CostModel",
    "EngineError",
    "EquityPoint",
    "EvalContext",
    "Fill",
    "HandOver",
    "Indicator",
    "InstrumentSpec",
    "LookaheadError",
    "Money",
    "NoCostModel",
    "OrderRequest",
    "OrderResult",
    "PercentRiskManager",
    "Portfolio",
    "Position",
    "RiskManager",
    "RunResult",
    "Side",
    "Signal",
    "SignalKind",
    "SpreadCostModel",
    "Strategy",
    "SwapRates",
    "Volume",
    "__version__",
    "build_indicator",
    "compile_strategy",
    "compute_metrics",
    "hand_over",
    "iter_run",
    "run",
    "unwarmed_indicators",
]

# ⚠️ **Raised by every change that can move a result** (28/09). A finished run is reused for a new
# one only under the same version (`tradeforge_api.reuse`), so a fix that changes a trade and keeps
# the number would hand back results the engine no longer gives. It sat at 0.1.0 from July to
# 28/09 through many such fixes; nothing run under 0.1.0 is reused.
__version__ = "0.3.0"
