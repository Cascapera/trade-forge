"""Live sessions: the engine driven by candles that have not happened yet.

`packages/engine` already runs live and does not know it — `loop.iter_run` takes an `Iterable`
of candles and pulls one at a time, so the difference between a backtest and a paper session is
entirely in what that iterable is. This package is that iterable, and nothing else.
"""

from tradeforge_api.live.candle_stream import CandleStream, StreamReader
from tradeforge_api.live.heartbeat import Heartbeat, session_heartbeat
from tradeforge_api.live.recorder import BarChanges, LedgerWatch, TradeRecorder, record_bar
from tradeforge_api.live.splice import BarSource, SplicedCandles, splice

__all__ = [
    "BarChanges",
    "BarSource",
    "CandleStream",
    "Heartbeat",
    "LedgerWatch",
    "SplicedCandles",
    "StreamReader",
    "TradeRecorder",
    "record_bar",
    "session_heartbeat",
    "splice",
]
