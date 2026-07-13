"""MetaTrader 5 execution service — the second Windows-bound edge.

Consumes order requests from Redis, sends them to MT5, publishes fills, and keeps
an append-only audit trail. Its safeguards (kill switch, daily loss cap, position
and volume limits) are deliberately local: they must hold even if the core is
down (sdd.md §3.3.3, §11).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
