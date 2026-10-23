"""Live sessions: the engine driven by candles that have not happened yet.

`packages/engine` already runs live and does not know it — `loop.iter_run` takes an `Iterable`
of candles and pulls one at a time, so the difference between a backtest and a paper session is
entirely in what that iterable is. For a **paper** session this package is that iterable, and
nothing else.

A session against a real venue needs one thing more, and only one: a `Broker` whose orders leave
the process. That is `MT5Broker` — the second seam, filled in the same way, by handing the
engine an object that satisfies a protocol written before either implementation existed.
"""

from tradeforge_api.live.broker import MT5Broker, OrderWire
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
    "MT5Broker",
    "OrderWire",
    "SplicedCandles",
    "StreamReader",
    "TradeRecorder",
    "record_bar",
    "session_heartbeat",
    "splice",
]
