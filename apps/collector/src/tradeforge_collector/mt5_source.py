"""The real feed. Windows only — this module is never imported on Linux.

Nothing else in the collector imports it, and `__init__.py` does not either: the
import of `MetaTrader5` happens inside `connect()`, at the moment someone actually
asks for real data. That is what lets CI import the rest of the package, run the whole
backfill against `SyntheticSource`, and never touch a library it cannot install.

Two things here are *pure functions* on purpose — `asset_class_from_path` and
`infer_server_offset`. They hold the only two pieces of MT5 behaviour that can be
gotten wrong silently, so they are lifted out of the I/O and tested directly.
"""

import datetime as dt
import logging
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

from tradeforge_collector.source import Candle
from tradeforge_collector.storage import normalise
from tradeforge_db.instruments import InstrumentSpec
from tradeforge_db.models import AssetClass

logger = logging.getLogger(__name__)

# MT5 groups symbols in a tree: "Forex\\Majors\\EURUSD", "Stocks\\US\\AAPL".
_PATH_TO_ASSET_CLASS: dict[str, AssetClass] = {
    "forex": AssetClass.FOREX,
    "stocks": AssetClass.STOCK,
    "shares": AssetClass.STOCK,
    "indices": AssetClass.INDEX,
    "indexes": AssetClass.INDEX,
    "futures": AssetClass.FUTURE,
    "crypto": AssetClass.CRYPTO,
}

# Brokers run their servers a couple of hours ahead of UTC, and most of them observe
# daylight saving — so the offset is not even constant across a year. Rounded to the
# half hour because a few seconds of clock skew between the two machines must not turn
# into a one-hour correction.
_OFFSET_GRANULARITY = dt.timedelta(minutes=30)


def asset_class_from_path(path: str) -> AssetClass | None:
    """Read the asset class out of the symbol's tree path. `None` when it cannot tell.

    Deliberately returns `None` rather than guessing: an instrument filed under the
    wrong class gets the wrong tick arithmetic, and a wrong P&L that nobody questions is
    worse than a backfill that stops and asks.
    """
    head = path.replace("/", "\\").split("\\")[0].strip().lower()
    return _PATH_TO_ASSET_CLASS.get(head)


def infer_server_offset(server_time: dt.datetime, real_now: dt.datetime) -> dt.timedelta:
    """How far the broker's clock runs ahead of UTC.

    MT5 reports candle times as epoch seconds — but the epoch it means is *server local
    time labelled as if it were UTC*. There is no API that states the offset, so it is
    measured: take the server's current tick time, compare it to the real clock, and
    round to the nearest half hour.

    Get this wrong and every bar in the file is shifted by two or three hours. Nothing
    crashes. The backtest simply trades a market that was displaced in time — and the
    result looks perfectly plausible.
    """
    drift = server_time - real_now
    units = round(drift / _OFFSET_GRANULARITY)
    return units * _OFFSET_GRANULARITY


class MT5Source:
    """`MarketDataSource` backed by a running MetaTrader 5 terminal."""

    def __init__(
        self,
        *,
        asset_class: AssetClass | None = None,
        terminal: Any = None,  # noqa: ANN401 — MetaTrader5 ships no type stubs
    ) -> None:
        # An override for symbols whose tree path says nothing useful.
        self._asset_class = asset_class

        # The terminal is injectable purely so the conversion logic can be tested. That
        # logic — shifting the broker's clock to UTC, quantising a float to the tick — is
        # the code in this file most able to corrupt data without raising anything, so it
        # is the code that most needs to run in CI, on Linux, with no terminal in sight.
        self._terminal: Any = terminal
        self._mt5: Any = None
        self._offset = dt.timedelta()

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> Self:
        """Attach to the terminal and measure its clock."""
        mt5 = self._terminal
        if mt5 is None:
            # Imported here and nowhere else. At module scope this line would make the
            # file unimportable on Linux, and with it the whole package (ADR-02).
            import MetaTrader5  # noqa: PLC0415 — the ADR-02 boundary, deliberately late

            mt5 = MetaTrader5

        if not mt5.initialize():
            raise ConnectionError(f"MetaTrader 5 refused the connection: {mt5.last_error()}")

        self._mt5 = mt5
        self._offset = self._measure_offset()
        logger.info("connected to MetaTrader 5; server clock is UTC%+g h", self._offset / _HOUR)
        return self

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    def instrument(self, symbol: str) -> InstrumentSpec:
        mt5 = self._require_connection()

        info = mt5.symbol_info(symbol)
        if info is None:
            raise LookupError(f"symbol {symbol!r} is not available in this terminal")

        asset_class = self._asset_class or asset_class_from_path(info.path)
        if asset_class is None:
            raise LookupError(
                f"cannot tell the asset class of {symbol!r} from its path {info.path!r}; "
                f"pass --asset-class explicitly"
            )

        digits = int(info.digits)
        return InstrumentSpec(
            symbol=info.name,
            name=info.description or info.name,
            asset_class=asset_class,
            exchange=info.exchange or None,
            currency_base=info.currency_base or None,
            currency_quote=info.currency_profit,
            tick_size=normalise(Decimal(str(info.trade_tick_size)), digits),
            tick_value=Decimal(str(info.trade_tick_value)),
            contract_size=Decimal(str(info.trade_contract_size)),
            digits=digits,
        )

    def candles(
        self, symbol: str, timeframe: str, start: dt.datetime, end: dt.datetime
    ) -> list[Candle]:
        mt5 = self._require_connection()
        spec = self.instrument(symbol)

        # The range is asked for in the server's own clock, because that is the only
        # clock MT5 speaks — so shift our UTC bounds *into* server time on the way in,
        # and shift every returned bar back *out* of it on the way home.
        rates = mt5.copy_rates_range(
            symbol,
            self._timeframe_constant(mt5, timeframe),
            _naive(start + self._offset),
            _naive(end + self._offset),
        )
        if rates is None:
            raise LookupError(f"MT5 returned no rates for {symbol} {timeframe}: {mt5.last_error()}")

        return [
            Candle(
                time=dt.datetime.fromtimestamp(int(rate["time"]), tz=dt.UTC) - self._offset,
                open=normalise(Decimal(str(rate["open"])), spec.digits),
                high=normalise(Decimal(str(rate["high"])), spec.digits),
                low=normalise(Decimal(str(rate["low"])), spec.digits),
                close=normalise(Decimal(str(rate["close"])), spec.digits),
                tick_volume=int(rate["tick_volume"]),
                spread=int(rate["spread"]),
                real_volume=int(rate["real_volume"]),
            )
            for rate in rates
        ]

    def _measure_offset(self) -> dt.timedelta:
        mt5 = self._require_connection()

        # Any symbol will do: the tick carries the server's clock, which is what we are
        # actually asking about.
        for symbol in mt5.symbols_get() or []:
            tick = mt5.symbol_info_tick(symbol.name)
            if tick is not None and tick.time:
                server_now = dt.datetime.fromtimestamp(int(tick.time), tz=dt.UTC)
                return infer_server_offset(server_now, dt.datetime.now(tz=dt.UTC))

        logger.warning("no tick available to measure the server clock; assuming UTC")
        return dt.timedelta()

    def _require_connection(self) -> Any:  # noqa: ANN401 — MetaTrader5 ships no type stubs
        if self._mt5 is None:
            raise RuntimeError(
                "not connected: call connect() or use MT5Source as a context manager"
            )
        return self._mt5

    @staticmethod
    def _timeframe_constant(mt5: Any, timeframe: str) -> int:  # noqa: ANN401 — see above
        constant = getattr(mt5, f"TIMEFRAME_{timeframe}", None)
        if constant is None:
            raise ValueError(f"MetaTrader 5 has no timeframe {timeframe!r}")
        return int(constant)


_HOUR = dt.timedelta(hours=1)


def _naive(moment: dt.datetime) -> dt.datetime:
    """MT5 wants naive datetimes and reads them as server-local time."""
    return moment.replace(tzinfo=None)
