"""The candle stream and the timeframe must agree, or the lookahead ceiling means nothing.

This is the subtle one, and it is a bug that the *fix* for lookahead introduced. The ceiling
is `[candle.time, candle.time + timeframe)` — so its tightness is entirely a function of
`timeframe` being true. And `timeframe` is a parameter the caller supplies while the candles
come from somewhere else entirely.

The worker in PR-107 will read the timeframe from a saved strategy document and the candles
from a Parquet dataset. Those are two different sources. Nothing but this check makes them
agree — and if they disagree, the guard that was written not to trust the broker quietly
starts trusting the caller instead.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from tradeforge_engine.domain import Candle, Fill
from tradeforge_engine.errors import EngineError, LookaheadError
from tradeforge_engine.loop import run
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    FixedRisk,
    ImmediateFillBroker,
    ScriptedStrategy,
    entry,
    rising,
)

FIVE_MINUTES = dt.timedelta(minutes=5)
DAY = dt.timedelta(days=1)


class FillsOneBarLate(ImmediateFillBroker):
    """The off-by-one: `candles[i+1].open` instead of `candles[i].open`."""

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills = [
            Fill(
                order=order,
                time=candle.time + HOUR,
                price=candle.close,
                volume=order.volume,
                costs=Decimal(0),
            )
            for order in self._pending
        ]
        self._pending.clear()
        return fills


def test_an_hourly_timeframe_over_hourly_candles_is_fine() -> None:
    result = run(
        candles=rising(5),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script={1: [entry()]}),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 5


def test_an_understated_timeframe_is_allowed_because_it_only_tightens_the_ceiling() -> None:
    """The asymmetry, stated on purpose. Hourly candles declared as five-minute bars pass.

    They pass because sixty minutes *is* a whole number of five-minute bars — and, more to the
    point, because an understated timeframe is **safe**: the ceiling becomes
    `[t, t + 5min)`, which is narrower than the truth. A too-tight ceiling cannot admit a fill
    from the future; at worst it rejects a legitimate intrabar one, loudly.

    The dangerous direction is the other one, and it is the one the check exists for: a
    timeframe *larger* than the bars dilates the ceiling over the bars that follow, and the
    modulo always catches that (a gap smaller than the timeframe is never a multiple of it).
    """
    result = run(
        candles=rising(5),
        timeframe=FIVE_MINUTES,
        instrument=EURUSD,
        strategy=ScriptedStrategy(),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 5


def test_an_oversized_timeframe_cannot_be_used_to_widen_the_ceiling() -> None:
    """The attack this check exists to stop.

    A broker that fills one bar into the future is caught by the ceiling — but only if the
    ceiling is one bar wide. Declare a daily timeframe over hourly candles and the ceiling
    stretches to cover the next twenty-four bars, and the off-by-one sails through it.

    With the stream checked against the timeframe, the run never gets far enough to find out.
    """
    with pytest.raises(EngineError, match="the stream and the timeframe disagree"):
        run(
            candles=rising(5),
            timeframe=DAY,
            instrument=EURUSD,
            strategy=ScriptedStrategy(script={1: [entry()]}),
            broker=FillsOneBarLate(),
            risk=FixedRisk(),
        )


def test_with_the_true_timeframe_the_off_by_one_broker_is_caught() -> None:
    """And this is what the check is protecting: the ceiling does its job when it is honest."""
    with pytest.raises(LookaheadError, match="outside the candle being processed"):
        run(
            candles=rising(5),
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=ScriptedStrategy(script={1: [entry()]}),
            broker=FillsOneBarLate(),
            risk=FixedRisk(),
        )


def test_a_weekend_gap_is_a_whole_number_of_bars_and_is_accepted() -> None:
    """Friday 21:00 to Monday 00:00 is 51 hours — 51 H1 bars. The market being shut is not a
    broken stream, and the check must not confuse the two.
    """
    friday = Candle(
        time=dt.datetime(2024, 1, 5, 21, tzinfo=dt.UTC),
        open=Decimal("1.10000"),
        high=Decimal("1.10100"),
        low=Decimal("1.09900"),
        close=Decimal("1.10050"),
    )
    monday = Candle(
        time=dt.datetime(2024, 1, 8, 0, tzinfo=dt.UTC),
        open=Decimal("1.10200"),
        high=Decimal("1.10300"),
        low=Decimal("1.10100"),
        close=Decimal("1.10250"),
    )

    result = run(
        candles=[friday, monday],
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(),
        broker=ImmediateFillBroker(),
        risk=FixedRisk(),
    )

    assert result.candles_processed == 2


def test_a_ragged_gap_is_refused() -> None:
    """Ninety minutes between two hourly bars is not a market closure. It is a broken dataset."""
    first = rising(1)[0]
    ragged = Candle(
        time=first.time + dt.timedelta(minutes=90),
        open=Decimal("1.10100"),
        high=Decimal("1.10200"),
        low=Decimal("1.10000"),
        close=Decimal("1.10150"),
    )

    with pytest.raises(EngineError, match="the stream and the timeframe disagree"):
        run(
            candles=[first, ragged],
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=ScriptedStrategy(),
            broker=ImmediateFillBroker(),
            risk=FixedRisk(),
        )


def test_a_zero_timeframe_is_refused() -> None:
    with pytest.raises(ValueError, match="timeframe must be positive"):
        run(
            candles=rising(3),
            timeframe=dt.timedelta(0),
            instrument=EURUSD,
            strategy=ScriptedStrategy(),
            broker=ImmediateFillBroker(),
            risk=FixedRisk(),
        )
