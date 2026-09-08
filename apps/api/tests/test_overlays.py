"""The zone half of `/backtests/{id}/overlays`, without a database.

`_zones_of` replays a run's bars through its strategy and turns the regions it marked into the
records the browser draws. The mapping is short and it is the only wire between an engine that
knows what a region is and a chart that does not, so it gets a test of its own — the integration
tests that call the endpoint prove the route and the window, not this.

⚠️ **The strategy is a stand-in, and only the strategy.** `_zones_of` is what runs for real here:
the replay loop, the flat account it drives on, and the field-by-field mapping. Handing it a real
setup instead would need a stream long enough for a four-hour structure to break inside an hourly
one — ninety bars, in the engine's own fixture — to reach a mapping that is nine lines and knows
nothing about how the regions were found.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_api.routers import backtests as router
from tradeforge_api.routers.backtests import _Window, _zones_of
from tradeforge_db.models import Backtest, Instrument, Strategy
from tradeforge_engine.domain import AssetClass, Candle, Context, Signal, ZoneMark

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


class _MarkedTwice:
    """A `Zoned` strategy holding one region of the timeframe above and one of its own.

    Two labels rather than one: a run whose regions all carry the same label cannot tell a
    handler that copied the field from one that never touched it, because the schema's default
    is that same word ([[probe-igual-ao-default]]). The engine fills both explicitly — the
    labels here are the ones `StructureStrategy.zones()` produces under an H4 filter.
    """

    def __init__(self) -> None:
        self.bars = 0

    def on_bar(self, context: Context) -> Sequence[Signal]:
        self.bars += 1
        return ()

    def zones(self) -> Sequence[ZoneMark]:
        return (
            ZoneMark(
                kind="demand",
                top=Decimal("1.10500"),
                bottom=Decimal("1.10000"),
                from_time=START,
                confirmed_at=START + dt.timedelta(hours=4),
                mitigated_at=None,
                primary=True,
                label="H4",
            ),
            ZoneMark(
                kind="supply",
                top=Decimal("1.10200"),
                bottom=Decimal("1.10100"),
                from_time=START + dt.timedelta(hours=1),
                confirmed_at=START + dt.timedelta(hours=2),
                mitigated_at=START + dt.timedelta(hours=3),
                primary=False,
                label="zone",
            ),
        )


def _window(strategy: object) -> _Window:
    instrument = Instrument(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    candles = [
        Candle(
            time=START + dt.timedelta(hours=index),
            open=Decimal("1.10000"),
            high=Decimal("1.10100"),
            low=Decimal("1.09900"),
            close=Decimal("1.10050"),
        )
        for index in range(3)
    ]
    return _Window(
        backtest=Backtest(id=uuid.uuid4(), initial_capital=Decimal(10_000)),
        instrument=instrument,
        strategy=Strategy(id=uuid.uuid4(), definition={}),
        seen=len(candles),
        first=candles[0].time,
        last=candles[-1].time,
        candles=candles,
    )


@pytest.fixture(name="compiles_to")
def _compiles_to(monkeypatch: pytest.MonkeyPatch) -> Any:
    def use(strategy: object) -> None:
        monkeypatch.setattr(router, "compile_strategy", lambda _document: strategy)

    return use


def test_every_region_says_which_timeframe_it_came_from(compiles_to: Any) -> None:
    """⚠️ **The wire the whole picture hangs on.** A run under the higher-timeframe filter marks
    regions on two charts at once and they mean opposite things — the small ones are where the
    order rests, the big one is what released the side at all. The label is the only thing that
    crosses from the engine to the browser saying which is which.

    Both labels are asserted, and that is the point: with only the run's own regions here, the
    handler could stop copying the field entirely and the response would not change, because the
    schema's default is that same word. It takes a region of a *different* timeframe to tell a
    field that was carried from one that was never touched.
    """
    strategy = _MarkedTwice()
    compiles_to(strategy)

    zones = _zones_of(_window(strategy))

    assert [zone.label for zone in zones] == ["H4", "zone"]


def test_the_regions_keep_the_order_the_strategy_published_them_in(compiles_to: Any) -> None:
    """The strategy puts the regions above first so a chart drawing in order leaves the big ones
    behind the small ones. A handler that sorted, grouped or de-duplicated would put the H4 band
    over the zone the order actually rests in, hiding it."""
    strategy = _MarkedTwice()
    compiles_to(strategy)

    zones = _zones_of(_window(strategy))

    assert [(zone.kind, zone.primary) for zone in zones] == [("demand", True), ("supply", False)]


def test_the_three_instants_and_the_band_survive_the_crossing(compiles_to: Any) -> None:
    """None of them is interchangeable: `from_time` is where the rectangle starts, `confirmed_at`
    is when the strategy could first act, `mitigated_at` is where it ends — and `None` there means
    still standing, which a chart draws to its own right edge rather than closing somewhere."""
    strategy = _MarkedTwice()
    compiles_to(strategy)

    above, own = _zones_of(_window(strategy))

    assert (above.top, above.bottom) == (Decimal("1.10500"), Decimal("1.10000"))
    assert above.from_time == START
    assert above.confirmed_at == START + dt.timedelta(hours=4)
    assert above.mitigated_at is None
    assert own.mitigated_at == START + dt.timedelta(hours=3)


def test_the_bars_are_replayed_through_the_strategy(compiles_to: Any) -> None:
    """A region is marked by a detector that needs the break the *same* bar produced, so the
    handler cannot ask the strategy without first driving it. Every bar of the window goes in."""
    strategy = _MarkedTwice()
    compiles_to(strategy)

    _zones_of(_window(strategy))

    assert strategy.bars == 3


def test_a_strategy_that_marks_no_regions_answers_with_an_empty_list(compiles_to: Any) -> None:
    """A swing setup enters off an average and is not `Zoned` at all. Empty is an answer."""

    class _Curveless:
        def on_bar(self, context: Context) -> Sequence[Signal]:
            return ()

    strategy = _Curveless()
    compiles_to(strategy)

    assert _zones_of(_window(strategy)) == []
