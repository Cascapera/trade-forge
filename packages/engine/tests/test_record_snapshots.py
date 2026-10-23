"""A run that records no pictures: `run(..., record_snapshots=False)`, for a sweep's runs.

The picture an entry leaves behind (`test_entry_snapshot.py`) is the one thing in the engine that
exists to be looked at rather than computed with, and a sweep of a million runs looks at none of
them one by one (2026-09-23). Switching it off is only safe if it is **only** a record-keeping
switch — so the claim pinned here is the strong one: over random markets, on every setup the DSL
can name and on both sides, a run without pictures is the same run, fill for fill, trade for trade,
bar of equity for bar of equity. The only difference allowed is the picture itself.
"""

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import Candle, ClosedTrade, Fill
from tradeforge_engine.loop import RunResult, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.testing import AAPL, HOUR, bar

# Every setup the DSL can name. The swing family on a three-bar average so random markets turn it
# often; the structure family on its own defaults, which is what a sweep would start from.
_SETUPS: dict[str, dict[str, object]] = {
    "mme9_breakout": {"period": 3},
    "mme9_turn": {"period": 3},
    "mme9_failed_turn": {"period": 3},
    "mme9_pullback": {"period": 3},
    "ponto_continuo": {"period": 3},
    "structure_choch": {},
    "structure_continuation": {},
}


@st.composite
def _random_walk(draw: st.DrawFn) -> list[Candle]:
    """Valid candles that wander, long enough for the structure setups to find swings."""
    count = draw(st.integers(min_value=80, max_value=300))
    # A drift, so some markets trend and the structure setups find breaks to trade.
    drift = draw(st.decimals(min_value="-0.8", max_value="0.8", places=1))
    step = st.decimals(min_value="-3", max_value="3", places=1)
    wick = st.decimals(min_value="0", max_value="1.5", places=1)
    candles: list[Candle] = []
    price = Decimal(200)
    for index in range(count):
        open_ = price
        close = max(Decimal(20), open_ + drift + draw(step))
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
        price = close
    return candles


def _run(kind: str, side: str, candles: list[Candle], *, record_snapshots: bool) -> RunResult:
    strategy = build_setup({"type": kind, "params": {"side": side, **_SETUPS[kind]}})
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=strategy,
        broker=BacktestBroker(
            instrument=AAPL, initial_capital=Decimal(100_000), take_profit_rr=Decimal(2)
        ),
        risk=PercentRiskManager(percent=Decimal(1)),
        record_snapshots=record_snapshots,
    )


def _without_picture(trade: ClosedTrade) -> ClosedTrade:
    return dataclasses.replace(trade, snapshot=None)


def _fill_without_picture(fill: Fill) -> Fill:
    return dataclasses.replace(fill, order=dataclasses.replace(fill.order, snapshot=None))


@pytest.mark.parametrize("kind", list(_SETUPS))
@pytest.mark.parametrize("side", ["long", "short", "both"])
@settings(max_examples=40, deadline=None)
@given(candles=_random_walk())
def test_snapshots_off_change_nothing_but_the_snapshots(
    kind: str, side: str, candles: list[Candle]
) -> None:
    """⚠️ The claim the sweep's saving rests on. If a picture ever fed a decision — a strategy
    reading its own window, a broker pricing off the splice — the two runs would part here, and
    a sweep would be measuring a different strategy from the one a single backtest shows.

    ⚠️ **How much of this is exercised, measured rather than hoped** (2026-09-23, 40 examples, side
    `both`): the four MME9 setups trade in 37 of them and the CHOCH in 9 — which covers both kinds
    of picture, curves and levels. The Ponto Contínuo and the continuation trade in almost none: a
    random walk rarely qualifies them. They stay in the list because the switch sits in the loop,
    where no setup can see it, and a setup that learned to would fail here the day it traded.
    """
    kept = _run(kind, side, candles, record_snapshots=True)
    dropped = _run(kind, side, candles, record_snapshots=False)

    assert [_without_picture(t) for t in dropped.trades] == [
        _without_picture(t) for t in kept.trades
    ]
    assert [_fill_without_picture(f) for f in dropped.fills] == [
        _fill_without_picture(f) for f in kept.fills
    ]
    assert dropped.equity_curve == kept.equity_curve
    assert dropped.refusals == kept.refusals
    assert dropped.final_account == kept.final_account

    assert all(trade.snapshot is None for trade in dropped.trades)
    assert all(fill.order.snapshot is None for fill in dropped.fills)


def test_by_default_every_trade_keeps_its_picture() -> None:
    """The switch is off only when asked: a single backtest's screen draws every entry, and a
    default that dropped them would blank it with nothing failing."""
    candles = [
        bar(index, open_=close, close=close, high=str(float(close) + 1), low=str(float(close) - 1))
        for index, close in enumerate(["100", "99", "98", "97", "96", "104", "106", "108", "103"])
    ]
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=build_setup({"type": "mme9_breakout", "params": {"side": "long", "period": 3}}),
        broker=BacktestBroker(
            instrument=AAPL, initial_capital=Decimal(100_000), take_profit_rr=Decimal(2)
        ),
        risk=PercentRiskManager(percent=Decimal(1)),
    )

    assert result.trades
    assert all(trade.snapshot is not None for trade in result.trades)
