"""`MarketReading` (ADR-0029): one reading of the market, advanced once a bar, read by many setups.

The claim a batch of sweep runs rests on: a structure setup that **reads** a shared reading trades
exactly as the same setup reading its own — trade for trade, point for point of equity. Held here
over seeded random walks, with the reading advanced by a leader ahead of every setup's bar, the
way the batch will drive it.
"""

import datetime as dt
import random
from decimal import Decimal, localcontext
from typing import Any

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.higher_timeframe import HigherTimeframeGate, RegionTracker
from tradeforge_engine.loop import ENGINE_CONTEXT, iter_run
from tradeforge_engine.reading import MarketReading
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import _structure_kwargs
from tradeforge_engine.setups import ChochQualifier, ContinuationQualifier, StructureStrategy
from tradeforge_engine.testing import EURUSD, HOUR, START

H4 = 4 * HOUR

# The points of a small grid, as a sweep would vary them over one market: entry, side, the
# breakeven, the continuation's ladder. Each one is a setup that shares the reading.
_POINTS: list[dict[str, Any]] = [
    {"entry_point": entry, "side": side, "breakeven_at_r": breakeven, "max_bos": max_bos}
    for entry in ("edge", "midpoint", "return_pass", "martelo")
    for side in ("both", "long")
    for breakeven in (None, 1)
    for max_bos in (None, 1)
]


def _walk(seed: int, count: int = 1500) -> list[Candle]:
    """Hourly candles around 1.1 that wander and trend — a market that breaks structure often."""
    rng = random.Random(seed)  # noqa: S311 — a reproducible walk, not a secret
    price = Decimal("1.10000")
    drift = Decimal(rng.randint(-3, 3)) / 100_000
    candles = []
    impulse, push = 0, 0
    for index in range(count):
        # Now and then an impulse: three to five wide bars one way, which leave the gaps his
        # regions are marked by, and the breaks of structure that reveal them.
        if impulse == 0 and rng.random() < 0.04:
            impulse, push = rng.randint(3, 5), rng.choice([-1, 1]) * rng.randint(80, 160)
        move = push if impulse else rng.randint(-60, 60)
        impulse = max(0, impulse - 1)
        wick = rng.randint(0, 8) if impulse else rng.randint(0, 25)
        open_ = price
        close = max(Decimal("0.50000"), open_ + drift + Decimal(move) / 100_000)
        high = max(open_, close) + Decimal(wick) / 100_000
        low = min(open_, close) - Decimal(rng.randint(0, 25)) / 100_000
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=open_,
                high=high,
                low=low,
                close=close,
                tick_volume=100 + rng.randint(0, 900),
            )
        )
        price = close
    return candles


def _setup(point: dict[str, Any], *, htf: bool, reading: MarketReading | None) -> StructureStrategy:
    params: dict[str, object] = {
        "entry_point": point["entry_point"],
        "side": point["side"],
        "breakeven_at_r": point["breakeven_at_r"],
        **({"htf": "H4", "htf_offset": 0} if htf else {}),
    }
    kwargs = _structure_kwargs(params, HOUR)
    if point["max_bos"] is None:
        return StructureStrategy(
            qualifier=ChochQualifier(), name="choch", reading=reading, **kwargs
        )
    return StructureStrategy(
        qualifier=ContinuationQualifier(max_bos=point["max_bos"]),
        name="continuation",
        reading=reading,
        **kwargs,
    )


def _broker() -> BacktestBroker:
    return BacktestBroker(
        instrument=EURUSD, initial_capital=Decimal(10_000), take_profit_rr=Decimal(2)
    )


class _Feed:
    """A candle source the batch fills one bar at a time — what `iter_run` pulls from."""

    def __init__(self) -> None:
        self.bar: Candle | None = None

    def __iter__(self) -> "_Feed":
        return self

    def __next__(self) -> Candle:
        if self.bar is None:
            raise StopIteration
        bar, self.bar = self.bar, None
        return bar


# Seeds whose walk trades under each: a filter above lets through only a few of a walk's entries,
# so its seeds are the ones that trade at all (measured on 27/09 — 4 to 16 trades each).
_CASES = [(seed, True) for seed in (0, 4, 5, 10, 11)] + [(seed, False) for seed in range(6)]


@pytest.mark.parametrize(("seed", "htf"), _CASES)
def test_setups_reading_one_shared_reading_trade_as_they_do_alone(seed: int, htf: bool) -> None:
    candles = _walk(seed)

    # ⚠️ **The releases too, bar by bar, not only the trades.** A setup on its own advances its
    # reading and only then lets its gate read what the bar reached; the two lines swapped read the
    # bar before's touches, which the guardian measured changing the releases on 96 of 96 walks
    # and not one trade (27/09). A shared reading is ahead by construction, so it is the reference.
    alone = []
    for point in _POINTS:
        setup = _setup(point, htf=htf, reading=None)
        broker = _broker()
        releases = []
        curve = []
        for outcome in iter_run(
            candles=candles,
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=setup,
            broker=broker,
            risk=PercentRiskManager(percent=Decimal(1)),
            record_snapshots=False,
        ):
            curve.append(outcome.equity)
            releases.append(_releases_of(setup))
        alone.append((repr(broker.trades()), tuple(curve), tuple(releases)))

    reading = MarketReading(timeframe=HOUR, htf=H4 if htf else None, htf_offset=dt.timedelta(0))
    feeds = [_Feed() for _ in _POINTS]
    brokers = [_broker() for _ in _POINTS]
    setups = [_setup(point, htf=htf, reading=reading) for point in _POINTS]
    runs = [
        iter_run(
            candles=feed,
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=setup,
            broker=broker,
            risk=PercentRiskManager(percent=Decimal(1)),
            record_snapshots=False,
        )
        for setup, feed, broker in zip(setups, feeds, brokers, strict=True)
    ]
    curves: list[list[Any]] = [[] for _ in _POINTS]
    timelines: list[list[str]] = [[] for _ in _POINTS]
    for candle in candles:
        # The leader: once a bar, ahead of every setup, under the engine's own arithmetic.
        with localcontext(ENGINE_CONTEXT):
            reading.advance(candle)
        for setup, feed, bars, curve, timeline in zip(
            setups, feeds, runs, curves, timelines, strict=True
        ):
            feed.bar = candle
            curve.append(next(bars).equity)
            timeline.append(_releases_of(setup))

    shared = [
        (repr(broker.trades()), tuple(curve), tuple(timeline))
        for broker, curve, timeline in zip(brokers, curves, timelines, strict=True)
    ]
    assert shared == alone
    # The walk has to have traded, or the equality is between two empty lists.
    assert sum(trades != "()" for trades, _, _ in alone) > 0
    if htf:
        # And the gate has to have released, or the timelines are two rows of nothing.
        assert any("Release" in state for _, _, timeline in alone for state in timeline)


def _releases_of(setup: StructureStrategy) -> str:
    return "" if setup._gate is None else repr(sorted(setup._gate._releases.items()))


def test_a_reading_not_advanced_to_the_bar_is_refused() -> None:
    """A leader one bar behind would hand every run of the batch another bar's market."""
    candles = _walk(0, count=3)
    reading = MarketReading()
    setup = _setup(_POINTS[0], htf=False, reading=reading)
    feed = _Feed()
    bars = iter_run(
        candles=feed,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=setup,
        broker=_broker(),
        risk=PercentRiskManager(percent=Decimal(1)),
    )
    with localcontext(ENGINE_CONTEXT):
        reading.advance(candles[0])
    feed.bar = candles[0]
    next(bars)
    feed.bar = candles[1]
    with pytest.raises(EngineError, match="market reading is at"):
        next(bars)


def test_a_reading_ahead_of_the_bar_is_refused() -> None:
    """⚠️ The lookahead: a leader that ran ahead would hand a setup the next bar's breaks and
    regions on this one — a zone armed before it exists, a backtest better than its market."""
    candles = _walk(0, count=3)
    reading = MarketReading()
    feed = _Feed()
    bars = iter_run(
        candles=feed,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=_setup(_POINTS[0], htf=False, reading=reading),
        broker=_broker(),
        risk=PercentRiskManager(percent=Decimal(1)),
    )
    with localcontext(ENGINE_CONTEXT):
        reading.advance(candles[0])
        reading.advance(candles[1])
    feed.bar = candles[0]
    with pytest.raises(EngineError, match="market reading is at"):
        next(bars)


def test_a_reading_of_another_higher_timeframe_is_refused() -> None:
    with pytest.raises(ValueError, match="timeframe this setup filters by"):
        _setup(_POINTS[0], htf=True, reading=MarketReading())
    with pytest.raises(ValueError, match="timeframe this setup filters by"):
        _setup(
            _POINTS[0],
            htf=False,
            reading=MarketReading(timeframe=HOUR, htf=H4, htf_offset=dt.timedelta(0)),
        )
    with pytest.raises(ValueError, match="timeframe this setup filters by"):
        _setup(
            _POINTS[0],
            htf=True,
            reading=MarketReading(timeframe=HOUR, htf=H4, htf_offset=dt.timedelta(hours=3)),
        )


def test_a_gate_refuses_a_tracker_of_another_timeframe() -> None:
    tracker = RegionTracker(base=HOUR, target=H4, offset=dt.timedelta(0))
    with pytest.raises(ValueError, match="tracker reads"):
        HigherTimeframeGate(base=HOUR, target=2 * HOUR, offset=dt.timedelta(0), regions=tracker)


def test_what_the_tracker_reached_cannot_be_written_by_a_reader() -> None:
    """Read by every run of a batch: a write by one would take a region from all the others."""
    tracker = RegionTracker(base=HOUR, target=H4, offset=dt.timedelta(0))
    with pytest.raises(TypeError):
        tracker.reached[Side.LONG] = ()  # type: ignore[index]
