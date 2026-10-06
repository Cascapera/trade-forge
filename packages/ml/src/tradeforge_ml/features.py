"""What the market looked like when a setup entered — the variables a meta-label learns from
(ADR-0031, rule 1).

The event base says what a setup did and how it ended (`events`); this says, for each entry, what
the chart showed at the **decision bar**: the last bar fully closed before the entry filled. Every
variable is read from that bar and the bars before it, never from the bar the trade filled in or
any after it — a test changes every later bar and finds nothing moved (`tests/test_features.py`).

⚠️ **The decision bar is the last closed one, even for an order that rested.** A stop order placed
bars earlier fills inside a bar; the latest moment a filter could still act on it is the close of
the bar before, by cancelling the order there. So that bar, and not the one the order was placed
on, is what the model sees — it may know more than the setup did, never more than a trader could.

⚠️ **Oriented by the trade's side.** A variable that has a direction — distance to an average,
a return, the position in a range — is multiplied by +1 for a long and -1 for a short, so "with
the trend" reads the same for both and one model learns both sides.

⚠️ **Three indicators written here, not a library's.** The EMA, Wilder's ATR and RSI are a dozen
lines each and tested against values worked by hand; a library would bring its own warm-up rules
and edge cases, unread. This is not a second copy of a strategy (`sdd.md` §5.3): the setup still
decides every entry in the engine, and these only describe the market around it.

Pure: candles and events in, variables out. `feature_export` reads and writes.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt

from tradeforge_engine.domain import Candle

FEATURES_VERSION = 2
"""Bumped whenever a variable is added, removed or computed differently: a model records the
version it was trained on (ADR-0031, rule 7).

2 (06/10): the spread against its own recent past and against the stop, the trading sessions in
their cities' clocks, and how far the market moved inside the session under way."""

Floats = npt.NDArray[np.float64]
Instants = npt.NDArray[np.int64]
"""Bar opening times as UTC microseconds since the epoch — searched, never converted per bar."""

STEP: dict[str, dt.timedelta] = {
    "M1": dt.timedelta(minutes=1),
    "M5": dt.timedelta(minutes=5),
    "M15": dt.timedelta(minutes=15),
    "M30": dt.timedelta(minutes=30),
    "H1": dt.timedelta(hours=1),
    "H4": dt.timedelta(hours=4),
    "D1": dt.timedelta(days=1),
    "W1": dt.timedelta(weeks=1),
}
"""How long a bar of each chart lasts — the collector's own table (`TIMEFRAME_STEP`), written here
because a shared package may not import a deployable app (`tests/test_architecture.py`)."""

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def micros(instant: dt.datetime) -> int:
    """A UTC instant as microseconds since the epoch."""
    return (instant - _EPOCH) // dt.timedelta(microseconds=1)


def instant(us: int) -> dt.datetime:
    return _EPOCH + dt.timedelta(microseconds=us)


LONGEST = 200
"""The longest look-back a variable reads (EMA 200): a decision bar with fewer bars before it has
no value for the variables that need them, never a value computed on too little."""

STREAK_CAP = 10

SPREAD_LOOKBACK = 100
"""Bars the spread is measured against (`spread_rel`): its median over them, the decision bar's
included. Fewer than half of them with a spread recorded, and there is no value."""


SATURDAY = 5
"""`date.weekday()` of the first day no session opens on, in its own city."""


@dataclass(frozen=True)
class Session:
    """A trading session in its own city's clock — so a summer and a winter day open at the same
    local hour, which `hour_utc` cannot say: 13:00 UTC is New York's open half the year."""

    name: str
    zone: ZoneInfo
    opens: dt.time
    closes: dt.time

    def opening(self, at: dt.datetime) -> dt.datetime | None:
        """The UTC instant this session opened on `at`'s local day, when `at` falls inside it on a
        weekday; otherwise `None`. Open at its opening instant, closed at its closing one."""
        local = at.astimezone(self.zone)
        if local.weekday() >= SATURDAY or not self.opens <= local.time() < self.closes:
            return None
        return dt.datetime.combine(local.date(), self.opens, tzinfo=self.zone).astimezone(dt.UTC)


SESSIONS = (
    Session("asia", ZoneInfo("Asia/Tokyo"), dt.time(9), dt.time(18)),
    Session("london", ZoneInfo("Europe/London"), dt.time(8), dt.time(17)),
    Session("ny", ZoneInfo("America/New_York"), dt.time(8), dt.time(17)),
    Session("ny_cash", ZoneInfo("America/New_York"), dt.time(9, 30), dt.time(16)),
)
"""Forex's three sessions and New York's cash hours, when its stock indices trade for real."""

SESSION_MOVE_LONGEST_STEP = dt.timedelta(hours=1)
"""Above H1 a single bar straddles a session's opening, so how far the market moved since it
cannot be read: on H4, D1 and W1 the session's move is empty. The flags still are not."""

FEATURE_NAMES = (
    "atr_pct",
    "atr_ratio",
    "bar_range_atr",
    "stop_atr",
    "ema20_dist_atr",
    "ema50_dist_atr",
    "ema200_dist_atr",
    "ema50_slope_atr",
    "ema_stack",
    "ret1_atr",
    "ret5_atr",
    "ret20_atr",
    "rsi14",
    "range_pos20",
    "range_pos100",
    "body_ratio",
    "streak",
    "volume_ratio",
    "hour_utc",
    "weekday",
    "month",
    "bars_before",
    "spread_rel",
    "spread_stop",
    *(f"session_{session.name}" for session in SESSIONS),
    "session_ret_atr",
    "session_range_atr",
    "session_pos",
    "session_minutes",
)
"""The variables, in the order a row carries them."""


def ema(values: Floats, period: int) -> Floats:
    """The exponential average, `2 / (period + 1)`, seeded with the first value."""
    alpha = 2.0 / (period + 1)
    out = np.empty_like(values)
    if len(values) == 0:
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = out[i - 1] + alpha * (values[i] - out[i - 1])
    return out


def wilder(values: Floats, period: int) -> Floats:
    """Wilder's smoothing — a running average with weight `1 / period` — seeded with the first."""
    out = np.empty_like(values)
    if len(values) == 0:
        return out
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = out[i - 1] + (values[i] - out[i - 1]) / period
    return out


def true_range(high: Floats, low: Floats, close: Floats) -> Floats:
    """The bar's range, stretched to the previous close when the market gapped."""
    previous = np.concatenate(([close[0]], close[:-1])) if len(close) else close
    return np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))


def rsi(close: Floats, period: int = 14) -> Floats:
    """Wilder's RSI, 0 to 100; 50 where nothing moved."""
    change = np.diff(close, prepend=close[:1])
    gain = wilder(np.maximum(change, 0.0), period)
    loss = wilder(np.maximum(-change, 0.0), period)
    total = gain + loss
    with np.errstate(divide="ignore", invalid="ignore"):
        # Clipped: the division can land a rounding above 100 when every move was a gain.
        return np.clip(np.where(total > 0, 100.0 * gain / total, 50.0), 0.0, 100.0)


@dataclass(frozen=True, slots=True)
class Market:
    """One chart's bars as arrays, and the series every variable reads, computed once."""

    times: Instants
    step: dt.timedelta
    open: Floats
    high: Floats
    low: Floats
    close: Floats
    volume: Floats
    spread: Floats
    """In the broker's points, as the bar recorded it; `nan` where it recorded none (zero)."""
    point: float | None
    """What one point is in price (`10 ** -digits`), or `None` when the instrument is unknown."""
    atr14: Floats
    atr100: Floats
    ema20: Floats
    ema50: Floats
    ema200: Floats
    rsi14: Floats

    @classmethod
    def of(
        cls, candles: Sequence[Candle], step: dt.timedelta, point: float | None = None
    ) -> Market:
        """From the engine's bars — what a test builds."""

        def column(name: str) -> Floats:
            return np.array([float(getattr(bar, name)) for bar in candles], dtype=np.float64)

        return cls.from_arrays(
            times=np.array([micros(bar.time) for bar in candles], dtype=np.int64),
            step=step,
            open_=column("open"),
            high=column("high"),
            low=column("low"),
            close=column("close"),
            volume=column("tick_volume"),
            spread=column("spread"),
            point=point,
        )

    @classmethod
    def from_arrays(  # noqa: PLR0913 — keyword-only; one per column of the bars
        cls,
        *,
        times: Instants,
        step: dt.timedelta,
        open_: Floats,
        high: Floats,
        low: Floats,
        close: Floats,
        volume: Floats,
        spread: Floats | None = None,
        point: float | None = None,
    ) -> Market:
        """From columns in time order — what is read from the collector's Parquet."""
        ranges = true_range(high, low, close)
        recorded = np.full(len(close), np.nan) if spread is None else spread.astype(np.float64)
        # A zero is a bar the broker recorded no spread for (GOLD and BTCUSD before 2017-18), not
        # a free trade: read as one, it would teach that old bars cost nothing.
        recorded[recorded <= 0] = np.nan
        return cls(
            times=times,
            step=step,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            spread=recorded,
            point=point,
            atr14=wilder(ranges, 14),
            atr100=wilder(ranges, 100),
            ema20=ema(close, 20),
            ema50=ema(close, 50),
            ema200=ema(close, 200),
            rsi14=rsi(close),
        )

    def decision_bar(self, entry_time: dt.datetime) -> int | None:
        """The last bar closed by `entry_time` — its open plus one step at or before it — or
        `None` when the entry came before any bar had closed."""
        latest_open = micros(entry_time - self.step)
        index = int(np.searchsorted(self.times, latest_open, side="right")) - 1
        return None if index < 0 else index


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else float("nan")


def _position(close: float, highs: Floats, lows: Floats) -> float:
    top, bottom = float(highs.max()), float(lows.min())
    return _ratio(close - bottom, top - bottom)


def _spread(
    market: Market, i: int, entry_price: float, stop_loss: float | None
) -> dict[str, float]:
    """The decision bar's spread against its own last `SPREAD_LOOKBACK` bars, and in price against
    the stop. Never the spread in points itself: in this broker's history it moves in steps by year
    (EURUSD 5 since 2017, Usa500 23 from 2018 to 2024), so a model would learn the year from it."""
    nan = float("nan")
    now = float(market.spread[i])
    recent = market.spread[max(0, i + 1 - SPREAD_LOOKBACK) : i + 1]
    recorded = recent[~np.isnan(recent)]
    typical = float(np.median(recorded)) if len(recorded) * 2 >= SPREAD_LOOKBACK else nan
    risk = nan if stop_loss is None else abs(entry_price - stop_loss)
    return {
        "spread_rel": _ratio(now, typical) if not math.isnan(typical) else nan,
        "spread_stop": (
            _ratio(now * market.point, risk) if market.point is not None and risk > 0 else nan
        ),
    }


def _sessions(
    market: Market, i: int, decided: dt.datetime, sign: float, atr: float
) -> dict[str, float]:
    """Which sessions the decision falls in, and the market inside the one that opened last.

    The move is read from the session's first whole bar (the first opening at or after the
    session's) to the decision bar: on H1, New York's cash hours (09:30) start at the 10:00 bar.
    """
    nan = float("nan")
    openings = {session.name: session.opening(decided) for session in SESSIONS}
    out = {f"session_{name}": float(at is not None) for name, at in openings.items()}
    started = [at for at in openings.values() if at is not None]
    move = dict.fromkeys(
        ("session_ret_atr", "session_range_atr", "session_pos", "session_minutes"), nan
    )
    if not started or market.step > SESSION_MOVE_LONGEST_STEP:
        return out | move
    opened = max(started)
    move["session_minutes"] = (decided - opened) / dt.timedelta(minutes=1)
    first = int(np.searchsorted(market.times, micros(opened), side="left"))
    if first <= i:
        close = float(market.close[i])
        highs, lows = market.high[first : i + 1], market.low[first : i + 1]
        where = _position(close, highs, lows)
        move["session_ret_atr"] = sign * _ratio(close - float(market.open[first]), atr)
        move["session_range_atr"] = _ratio(float(highs.max() - lows.min()), atr)
        move["session_pos"] = where if sign > 0 else 1.0 - where
    return out | move


def features_at(
    market: Market,
    *,
    entry_time: dt.datetime,
    side: str,
    entry_price: float,
    stop_loss: float | None,
) -> dict[str, float]:
    """The variables of one entry, read at its decision bar. Every value is `nan` when there is no
    decision bar; the ones that look back further than the bars there are, too."""
    nan = float("nan")
    i = market.decision_bar(entry_time)
    if i is None:
        return dict.fromkeys(FEATURE_NAMES, nan)
    sign = 1.0 if side == "long" else -1.0
    close = float(market.close[i])
    atr = float(market.atr14[i])

    def needs(bars: int) -> bool:
        """Whether the decision bar has `bars` bars up to and including it."""
        return i + 1 >= bars

    enough = needs(LONGEST)
    bar_range = float(market.high[i] - market.low[i])

    def back(bars: int) -> float:
        return sign * _ratio(close - float(market.close[i - bars]), atr) if i >= bars else nan

    def distance(average: Floats) -> float:
        return sign * _ratio(close - float(average[i]), atr)

    streak = 0
    j = i
    while j >= 1 and streak < STREAK_CAP and sign * (market.close[j] - market.close[j - 1]) > 0:
        streak += 1
        j -= 1

    stack = 0.0
    if market.ema20[i] > market.ema50[i] > market.ema200[i]:
        stack = 1.0
    elif market.ema20[i] < market.ema50[i] < market.ema200[i]:
        stack = -1.0

    def position(bars: int) -> float:
        if not needs(bars):
            return nan
        where = _position(
            close, market.high[i + 1 - bars : i + 1], market.low[i + 1 - bars : i + 1]
        )
        return where if side == "long" else 1.0 - where

    recent_volume = market.volume[max(0, i - 19) : i + 1]
    decided = instant(int(market.times[i])) + market.step
    return {
        "atr_pct": _ratio(atr, close),
        "atr_ratio": _ratio(atr, float(market.atr100[i])) if needs(100) else nan,
        "bar_range_atr": _ratio(bar_range, atr),
        "stop_atr": nan if stop_loss is None else _ratio(abs(entry_price - stop_loss), atr),
        "ema20_dist_atr": distance(market.ema20) if needs(20) else nan,
        "ema50_dist_atr": distance(market.ema50) if needs(50) else nan,
        "ema200_dist_atr": distance(market.ema200) if enough else nan,
        "ema50_slope_atr": (
            sign * _ratio(float(market.ema50[i] - market.ema50[i - 10]), atr) if needs(60) else nan
        ),
        "ema_stack": sign * stack if enough else nan,
        "ret1_atr": back(1),
        "ret5_atr": back(5),
        "ret20_atr": back(20),
        "rsi14": float(market.rsi14[i]) if side == "long" else 100.0 - float(market.rsi14[i]),
        "range_pos20": position(20),
        "range_pos100": position(100),
        "body_ratio": _ratio(abs(close - float(market.open[i])), bar_range),
        "streak": float(streak),
        "volume_ratio": _ratio(float(market.volume[i]), float(recent_volume.mean())),
        "hour_utc": float(decided.hour),
        "weekday": float(decided.weekday()),
        "month": float(decided.month),
        "bars_before": float(i + 1),
        **_spread(market, i, entry_price, stop_loss),
        **_sessions(market, i, decided, sign, atr),
    }


__all__ = [
    "FEATURES_VERSION",
    "FEATURE_NAMES",
    "LONGEST",
    "SESSIONS",
    "STEP",
    "Market",
    "Session",
    "ema",
    "features_at",
    "rsi",
    "true_range",
    "wilder",
]
