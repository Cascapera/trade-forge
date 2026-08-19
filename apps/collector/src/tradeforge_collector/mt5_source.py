"""The real feed. Windows only — this module is never imported on Linux.

Nothing else in the collector imports it, and `__init__.py` does not either: the
import of `MetaTrader5` happens inside `connect()`, at the moment someone actually
asks for real data. That is what lets CI import the rest of the package, run the whole
backfill against `SyntheticSource`, and never touch a library it cannot install.

Three things here are *pure functions* on purpose — `asset_class_from_path`,
`infer_server_offset` and `offset_is_plausible`. They hold the only pieces of MT5
behaviour that can be gotten wrong silently, so they are lifted out of the I/O and
tested directly.
"""

import datetime as dt
import logging
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

from tradeforge_collector.storage import normalise
from tradeforge_engine.domain import AssetClass, Candle, InstrumentSpec

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

# A clock is a timezone, and the furthest any inhabited place sits from UTC is +14.
# Beyond this the number being measured is not a clock at all — see `offset_is_plausible`.
_MAX_PLAUSIBLE_OFFSET = dt.timedelta(hours=14)


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


def offset_is_plausible(offset: dt.timedelta) -> bool:
    """Could this drift be a timezone at all?

    `infer_server_offset` measures the gap between the broker's clock and ours by reading
    the timestamp of a tick. That reading *is* the server's clock only while the market is
    open. The moment it closes the last tick stops moving, and the very same subtraction
    quietly changes meaning: it starts measuring **how long the market has been shut**.

    From a single reading the two are indistinguishable, so this is the line drawn between
    a number that could be a timezone and one that certainly is not. Every closure long
    enough to matter falls outside it — a weekend is 48h+, and a stock market's overnight
    is 17h+.

    What it deliberately does *not* catch: a closure shorter than the band. Run a backfill
    two hours after the bell and the reading is off by two hours and looks perfectly
    ordinary. Separating those two cases needs a second reading seconds later, to see
    whether the tick is still advancing — which buys certainty with wall-clock time and
    nondeterminism, and this project's second invariant is determinism. `--server-offset`
    is the answer instead: state the number and nothing has to be inferred.
    """
    return abs(offset) <= _MAX_PLAUSIBLE_OFFSET


class MT5Source:
    """`MarketDataSource` backed by a running MetaTrader 5 terminal."""

    def __init__(
        self,
        *,
        asset_class: AssetClass | None = None,
        server_offset: dt.timedelta | None = None,
        terminal: Any = None,  # noqa: ANN401 — MetaTrader5 ships no type stubs
    ) -> None:
        # An override for symbols whose tree path says nothing useful.
        self._asset_class = asset_class

        # The broker's clock, stated instead of measured. With the market closed there is
        # nothing to measure from, and a backfill that has to wait for the opening bell to
        # be correct is not a backfill anyone can schedule.
        #
        # A stated offset is checked against the same band as a measured one: `+30` is a
        # typo whether a human typed it or a frozen tick implied it, and letting it through
        # here would displace every bar exactly as silently.
        if server_offset is not None and not offset_is_plausible(server_offset):
            raise ValueError(
                f"a server offset of {server_offset / _HOUR:+g} h is not a timezone; "
                f"brokers sit within {_MAX_PLAUSIBLE_OFFSET / _HOUR:g} h of UTC"
            )
        self._stated_offset = server_offset

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
        if self._stated_offset is not None:
            self._offset = self._stated_offset
            source = "stated"
        else:
            self._offset = self._measure_offset()
            source = "measured"
        logger.info(
            "connected to MetaTrader 5; server clock is UTC%+g h (%s)",
            self._offset / _HOUR,
            source,
        )
        return self

    def close(self) -> None:
        """Detach. The handle is dropped **first**, so a failed shutdown still detaches.

        ⚠️ Order matters here for one caller only, and it is the one that matters: `reconnect`
        runs after the terminal has already gone away, which is exactly when `shutdown()` is
        most likely to fail. Clearing the handle afterwards would leave the source believing it
        is still attached, and the next `connect()` would be layered on a connection that no
        longer exists.
        """
        mt5, self._mt5 = self._mt5, None
        if mt5 is not None:
            mt5.shutdown()

    def reconnect(self) -> None:
        """Drop whatever is left of the old attachment and initialise a new one.

        Only the mechanism lives here; how often to try, and for how long, is `live.run`'s.

        ⚠️ **A reconnection re-measures the broker's clock**, and with the market shut that
        measurement is refused rather than guessed (`_measure_offset`). So a loop that is meant
        to outlive a weekend has to be started with `--server-offset`: without it the terminal
        can come back perfectly healthy and the reconnection will still fail, correctly, until
        the market re-opens. Labelling bars with a number measured from a frozen tick would put
        the whole outage's worth of candles into the stream at the wrong instant.
        """
        try:
            self.close()
        except Exception:  # noqa: BLE001 — a dead terminal is precisely what this survives
            logger.debug("shutting the old connection down failed", exc_info=True)
        self.connect()

    def subscribe(self, symbol: str) -> None:
        """Put the symbol into Market Watch, so that asking for its bars can actually answer.

        ⚠️ **Measured, not defensive.** On this project's broker 5 of 9550 symbols were
        selected, and an unselected one answers `symbol_info_tick` with `None` and
        `copy_rates_from_pos` with nothing — for ever. "Nothing" is also the honest answer for
        a symbol whose session has not produced a bar yet, so the two are indistinguishable
        from the loop's side, and a watch on the wrong symbol would look like a quiet market
        until somebody checked by hand.

        This writes to the terminal, which is the reason it is a separate call rather than
        something `recent_closed` does on the way past: selecting a symbol is a change to the
        operator's Market Watch, and it belongs at the moment a watch is set up, once, not on
        every poll.
        """
        mt5 = self._require_connection()
        if not mt5.symbol_select(symbol, True):
            raise LookupError(
                f"MetaTrader 5 would not add {symbol!r} to Market Watch: {mt5.last_error()}. "
                f"A symbol that is not selected reports no bars for ever, which reads exactly "
                f"like a market that has not closed one yet."
            )

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

    def spread_points(self, symbol: str) -> Decimal | None:
        """The broker's quoted spread, converted from MT5 `point` into engine ticks.

        MT5 reports `symbol_info.spread` as a count of `point`, and `point` is not the same
        quantity as `trade_tick_size`. On this project's own broker they happen to coincide —
        AAPL quotes both at 0.01, the forex pairs both at 0.00001 — and code written on that
        coincidence would be a silent factor-of-ten wherever a venue sets a tick to several
        points. So the conversion is explicit: `spread · point / tick_size` is the distance in
        ticks, whatever the two are.

        ⚠️ **This is the spread *now*.** On a fixed-spread instrument (`spread_float` false,
        which is how this broker quotes AAPL) that is the whole story. On a floating one it is
        one reading of a number that widens at rollover and around news, so what lands in the
        catalogue is a representative default a human can override — never a guarantee, and
        never something to reconcile a live fill against.

        `None` rather than an exception when the terminal has nothing: a symbol that has not
        been selected into Market Watch answers with zeroes, and a backfill should catalogue
        the instrument regardless rather than fail over a field nothing is blocked on.
        """
        mt5 = self._require_connection()

        info = mt5.symbol_info(symbol)
        if info is None:
            raise LookupError(f"symbol {symbol!r} is not available in this terminal")

        point = Decimal(str(info.point))
        tick_size = Decimal(str(info.trade_tick_size))
        if point <= 0 or tick_size <= 0:
            logger.warning(
                "%s: broker reports point=%s tick_size=%s; cannot express a spread in ticks",
                symbol,
                point,
                tick_size,
            )
            return None

        age = self._quote_age(mt5, symbol)
        if age is not None and age > _STALE_QUOTE:
            # Measured the hard way: catalogued 11 ticks for AAPL 47 minutes after the US
            # close, against the 1 tick the same terminal quoted during the session. Brokers
            # widen at the close, and `symbol_info.spread` keeps reporting that widened
            # number for as long as the market stays shut. Storing it would have charged
            # eleven times the real cost, under a column that reads as a measurement.
            #
            # `None` and not the number: unmeasured is the truthful answer, and the screen
            # already has words for it. A spread that looks measured is worse than one that
            # admits it is missing.
            logger.warning(
                "%s: last quote is %.0f min old — the market is shut and %s points is the "
                "widened closing spread, not the session's. Not catalogued; re-run while "
                "the market is open.",
                symbol,
                age.total_seconds() / 60,
                info.spread,
            )
            return None

        ticks = Decimal(int(info.spread)) * point / tick_size
        logger.info(
            "%s: spread %s points of %s = %s ticks of %s (%s)",
            symbol,
            info.spread,
            point,
            ticks,
            tick_size,
            "floating" if info.spread_float else "fixed",
        )
        return ticks

    def _quote_age(self, mt5: Any, symbol: str) -> dt.timedelta | None:  # noqa: ANN401 — no stubs
        """How long ago this symbol last quoted, in real time, or `None` if it never has.

        ⚠️ Only as good as `self._offset`. A tick carries the *server's* clock, so turning it
        into an age means undoing that offset — and when the offset was measured rather than
        stated, the very closure this is trying to detect is what corrupted it. Measured on
        this project's own terminal: 47 minutes after the close, the offset came out as +2 h
        instead of the true +3, which makes a 47-minute-old quote look one minute old.

        So this catches the case where the operator passed `--server-offset`, and cannot save
        the case where it had to guess. That is a real limit, not an oversight: the guard is
        worth having for the runs that state the offset, and `offset_is_plausible` already
        refuses the grosser version of the same problem.
        """
        tick = mt5.symbol_info_tick(symbol)
        if tick is None or not tick.time:
            return None
        quoted_at = dt.datetime.fromtimestamp(int(tick.time), tz=dt.UTC) - self._offset
        return dt.datetime.now(tz=dt.UTC) - quoted_at

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

        return [self._candle(rate, spec.digits) for rate in rates]

    def recent_closed(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """The last `count` bars that closed — asked for by **position**, never from a clock.

        `copy_rates_from_pos(start_pos=1, count=n)` is the whole answer: position 0 is the bar
        still forming and everything from position 1 back has closed. The terminal states which
        is which, so this method never has to decide it — and asking for several is the same
        question as asking for one, which is why there is no second method for the singular
        case.

        ⚠️ **The version that computes it instead is wrong for as long as the market is shut.**
        Comparing `bar.time + step <= now` needs `self._offset`, and that offset is measured
        from the newest tick — so a market that stopped ticking freezes the measurement and the
        comparison drifts by exactly the length of the stop. Measured on this broker on
        18/08/2026: five symbols all stopped at 22:59:59 server time, and 46 minutes later the
        measurement read +134 min against a real +180. Nothing raises; the loop simply decides
        wrong. The offset is still applied below, to *label* the bar in UTC — translating is not
        deciding.

        An empty list rather than an exception when there is no bar: a symbol the broker has
        never quoted and a session that has not produced its first bar are both ordinary states
        of the world, and a live loop that crashed on either would be a loop that cannot start
        before the open. The terminal being **gone** is not one of those states, and it raises —
        see `_require_terminal` for why that has to be checked before anything else is asked.
        """
        if count < 1:
            raise ValueError(f"a poll asks for at least one bar, got {count}")

        mt5 = self._require_connection()
        self._require_terminal(mt5)
        spec = self.instrument(symbol)

        rates = mt5.copy_rates_from_pos(symbol, self._timeframe_constant(mt5, timeframe), 1, count)
        if rates is None:
            return []

        # MT5 returns them newest-last already; the sort makes that a guarantee of this method
        # rather than an observation about a library, which is what every caller downstream —
        # the gap fill, and `XADD`'s refusal of a non-increasing id — is relying on.
        return sorted(
            (self._candle(rate, spec.digits) for rate in rates), key=lambda candle: candle.time
        )

    def _candle(self, rate: Any, digits: int) -> Candle:  # noqa: ANN401 — a numpy record, no stubs
        """One MT5 rate as a `Candle`, in UTC and quantised to the instrument's tick."""
        return Candle(
            time=dt.datetime.fromtimestamp(int(rate["time"]), tz=dt.UTC) - self._offset,
            open=normalise(Decimal(str(rate["open"])), digits),
            high=normalise(Decimal(str(rate["high"])), digits),
            low=normalise(Decimal(str(rate["low"])), digits),
            close=normalise(Decimal(str(rate["close"])), digits),
            tick_volume=int(rate["tick_volume"]),
            spread=int(rate["spread"]),
            real_volume=int(rate["real_volume"]),
        )

    def _require_terminal(self, mt5: Any) -> None:  # noqa: ANN401 — no stubs
        """Raise `ConnectionError` when the terminal itself has gone away.

        ⚠️ **Checked first, before the symbol is looked up, and that ordering is the point.** A
        dead terminal answers `symbol_info` with `None`, which this class already turns into
        "that symbol is not available in this terminal" — a sentence that is both wrong and
        convincing, and that the live loop would treat as one bad symbol rather than as the
        feed being down. Asking `terminal_info` up front means the ambiguous answers never get
        the chance to be read as the wrong thing.

        What it does **not** cover: a terminal that is running but logged out of the broker.
        That one still answers, with cached history that has stopped advancing, and telling it
        apart from a quiet market needs state over time — which belongs to whatever is watching
        the loop, not to a single call inside it.
        """
        if mt5.terminal_info() is None:
            raise ConnectionError(f"MetaTrader 5 is not answering: {mt5.last_error()}")

    def _measure_offset(self) -> dt.timedelta:
        mt5 = self._require_connection()

        newest = self._newest_tick(mt5)
        if newest is None:
            raise LookupError(
                "no symbol in this terminal has ever ticked, so the server's clock cannot "
                "be measured; pass --server-offset with the broker's offset from UTC"
            )

        offset = infer_server_offset(newest, dt.datetime.now(tz=dt.UTC))
        if not offset_is_plausible(offset):
            raise LookupError(
                f"the newest tick in this terminal is {newest:%Y-%m-%d %H:%M} in the "
                f"server's clock, {offset / _HOUR:+g} h from now — too far to be a "
                f"timezone. The market is almost certainly closed, which freezes the last "
                f"tick and makes this measurement the length of the closure instead of "
                f"the clock. Pass --server-offset (for example --server-offset +3)."
            )
        return offset

    def _newest_tick(self, mt5: Any) -> dt.datetime | None:  # noqa: ANN401 — no stubs
        """The most recent tick anywhere in the terminal, in the server's clock.

        The newest and not merely the first one found: with the market wide open, an
        illiquid symbol's last tick can be hours old while the terminal itself is
        perfectly live. Taking the maximum makes the reading as fresh as this terminal is
        able to be, which is the only part of the staleness problem that measurement can
        solve on its own.
        """
        newest = 0
        for symbol in mt5.symbols_get() or []:
            tick = mt5.symbol_info_tick(symbol.name)
            if tick is not None and tick.time:
                newest = max(newest, int(tick.time))

        return dt.datetime.fromtimestamp(newest, tz=dt.UTC) if newest else None

    def _require_connection(self) -> Any:  # noqa: ANN401 — MetaTrader5 ships no type stubs
        """The handle, or `ConnectionError` when this source is not attached to anything.

        ⚠️ **`ConnectionError` and not `RuntimeError`, and a run against the real terminal is
        what settled it.** Detached is a connection state, so it has to raise the exception the
        live loop treats as "the feed is down" — otherwise it lands in the per-symbol handler,
        gets logged, and the loop carries on polling a source it can never reach.

        The path that makes this reachable is the one this file just added. `reconnect()` calls
        `close()`, which clears the handle, and then `connect()`; when `connect()` fails — a
        terminal that is still coming up, which is the *expected* case — the handle stays
        cleared. With the old `RuntimeError` every later poll was swallowed as one bad symbol
        and no further reconnection was ever attempted: **one failed retry wedged the collector
        for good**, silently. 176 tests and 22 mutants did not see it, because the fake source
        kept raising `ConnectionError` after a failed reconnection and the real one did not.

        It still covers the programmer error of never calling `connect()` at all, and the
        remedy the loop applies to it — call `connect()` — happens to be the right one.
        """
        if self._mt5 is None:
            raise ConnectionError(
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

# How old a quote may be before its spread stops describing the session. Generous on purpose:
# a thinly traded symbol can go minutes without a print while the market is wide open, and
# refusing those would trade a wrong number for a missing one. What this is aimed at is the
# closed market, where the gap is tens of minutes and the quote is the widened closing print.
_STALE_QUOTE = dt.timedelta(minutes=5)


def _naive(moment: dt.datetime) -> dt.datetime:
    """MT5 wants naive datetimes and reads them as server-local time."""
    return moment.replace(tzinfo=None)
