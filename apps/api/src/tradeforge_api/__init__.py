"""HTTP/WebSocket surface of TradeForge (FastAPI).

Runs on Linux/Docker. Backtests are dispatched to async workers, never executed
in the request path.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
