"""Event-driven strategy engine — the core of TradeForge.

The engine is the single place where strategy logic lives. It runs unchanged
against a ``BacktestBroker``, a ``PaperBroker`` or a live ``MT5Broker``, which is
what keeps backtest results and live results describing the same system.

Three invariants hold here and are enforced by tests (see AGENTS.md §5):

* **No lookahead** — a decision made on the close of candle N is filled at the
  open of candle N+1. Indicators only ever see closed candles.
* **Determinism** — the same input always produces the same output.
* **Broker agnosticism** — the engine never learns where an order is executed.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
