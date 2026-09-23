"""How far a trade went: its maximum favourable and adverse excursions (MFE, MAE), 2026-09-23.

Without ticks nobody knows the order of a bar's high and low, so the broker hands the ledger only
the part of each bar the position certainly lived through, and every doubt goes against the trade.
One golden per row of that rule, on a long and on its mirror:

| bar                         | favourable counted     | adverse counted   |
|-----------------------------|------------------------|-------------------|
| held the whole bar          | the extreme            | the extreme       |
| entered at the open         | the extreme            | the extreme       |
| entered inside the bar      | nothing past the entry | the extreme       |
| left at the stop            | nothing                | up to the fill    |
| left at the target          | up to the fill         | the extreme       |
| left at the open (strategy) | the fill               | the fill          |

So MFE can only err low and MAE only high — the side that lets a target be derived later without
claiming a hit the bars do not prove (`excursion.with_target`).

Every number is the broker's, read off these bars; the risk is 5 everywhere (entry 100, stop 95),
so an R is five points.
"""

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    Candle,
    ClosedTrade,
    Fill,
    OrderRequest,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.excursion import LADDER, gross_r, target_ladder, with_target
from tradeforge_engine.loop import run
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.testing import AAPL, HOUR, ScriptedStrategy, bar

_AXIS = Decimal(200)


def _bars(rows: list[tuple[str, str, str, str]]) -> list[Candle]:
    """Bars from `(open, high, low, close)`."""
    return [bar(i, open_=o, high=h, low=lo, close=c) for i, (o, h, lo, c) in enumerate(rows)]


def _mirror(candles: list[Candle]) -> list[Candle]:
    return [
        Candle(
            time=c.time,
            open=_AXIS - c.open,
            high=_AXIS - c.low,
            low=_AXIS - c.high,
            close=_AXIS - c.close,
        )
        for c in candles
    ]


def _flip(price: str, side: Side) -> Decimal:
    return Decimal(price) if side is Side.LONG else _AXIS - Decimal(price)


def _entry(side: Side, *, stop: str = "95", limit: str | None = None) -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=side,
        reference_price=_flip("100", side),
        stop_loss=_flip(stop, side),
        limit_price=None if limit is None else _flip(limit, side),
        client_id="e1",
    )


def _exit(side: Side) -> Signal:
    return Signal(kind=SignalKind.EXIT, side=side, reference_price=_flip("100", side))


def _trade(
    side: Side,
    rows: list[tuple[str, str, str, str]],
    script: dict[int, list[Signal]],
    *,
    rr: str | None = None,
) -> ClosedTrade:
    candles = _bars(rows) if side is Side.LONG else _mirror(_bars(rows))
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=ScriptedStrategy(script),
        broker=BacktestBroker(
            instrument=AAPL,
            initial_capital=Decimal(100_000),
            take_profit_rr=None if rr is None else Decimal(rr),
        ),
        risk=PercentRiskManager(percent=Decimal(1)),
    )
    [trade] = result.trades
    return trade


def _excursion(trade: ClosedTrade, side: Side) -> tuple[str, str, str, str]:
    """MFE and MAE as the long would print them, whatever the side, plus both in R."""
    assert trade.mfe_price is not None
    assert trade.mae_price is not None

    def back(price: Decimal) -> Decimal:
        return price if side is Side.LONG else _AXIS - price

    return (
        str(back(trade.mfe_price)),
        str(back(trade.mae_price)),
        str(trade.mfe_r),
        str(trade.mae_r),
    )


_SIDES = pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])

# Bar 0 decides; bar 1 enters at its open of 100 and ranges 99 to 104; bar 2 ranges 101 to 108.
_OPEN_AND_HOLD = [
    ("100", "101", "99", "100"),
    ("100", "104", "99", "102"),
    ("102", "108", "101", "107"),
]


@_SIDES
def test_a_bar_held_whole_counts_both_extremes_and_so_does_an_entry_at_the_open(
    side: Side,
) -> None:
    """Bar 3 stops the trade out at 95 — and its high of 110 is **not** counted: it may have come
    after the exit. What bars 1 and 2 were held through is: best 108, worst 99 — then the stop at
    95 takes the worst down to the fill."""
    rows = [*_OPEN_AND_HOLD, ("107", "110", "94", "95")]
    trade = _trade(side, rows, {0: [_entry(side)]})

    assert trade.reason == "sl"
    assert _excursion(trade, side) == ("108", "95", "1.6", "1")


@_SIDES
def test_a_stop_gapped_through_counts_the_fill_not_the_stop(side: Side) -> None:
    """The bar opens at 93, under the stop: the trade leaves at 93, and that is how far it went
    against — 1.4 R, not the 1 R the stop promised."""
    rows = [*_OPEN_AND_HOLD, ("93", "96", "90", "92")]
    trade = _trade(side, rows, {0: [_entry(side)]})

    assert _excursion(trade, side) == ("108", "93", "1.6", "1.4")


@_SIDES
def test_a_target_counts_up_to_the_fill_and_the_whole_adverse_side(side: Side) -> None:
    """A 2R target sits at 110. Bar 2 reaches 111 and leaves at 110: the favourable side counts to
    the fill, never to the 111 traded after it, and the bar's low of 101 counts against — it may
    have come before the target."""
    rows = [("100", "101", "99", "100"), ("100", "104", "99", "102"), ("102", "111", "101", "109")]
    trade = _trade(side, rows, {0: [_entry(side)]}, rr="2")

    assert trade.reason == "tp"
    assert _excursion(trade, side) == ("110.00", "99", "2", "0.2")


@_SIDES
def test_an_exit_at_the_open_saw_only_the_open(side: Side) -> None:
    """The strategy's exit, decided on bar 2, leaves at bar 3's open of 109 — a gap past the best
    price held so far, which the exit itself proves was traded. The rest of bar 3, 110 up and 103
    down, was traded by nobody's position."""
    rows = [*_OPEN_AND_HOLD, ("109", "110", "103", "105")]
    trade = _trade(side, rows, {0: [_entry(side)], 2: [_exit(side)]})

    assert _excursion(trade, side) == ("109.00", "99", "1.8", "0.2")


@_SIDES
def test_an_entry_inside_the_bar_cannot_claim_that_bars_favourable_extreme(side: Side) -> None:
    """A buy limit at 98 fills inside bar 1 (open 100, low 97): the high of 104 may predate the
    fill, so it is not the position's — but the close of 103.5 is the bar's last tick, after any
    fill, so it is. Bar 2 is held whole and only reaches 103: a best of 104 would be bar 1's high
    wrongly claimed, and one of 103 would be its close wrongly left out."""
    rows = [
        ("100", "101", "99", "100"),
        ("100", "104", "97", "103.5"),
        ("102", "103", "101", "102.5"),
        ("102.5", "103", "90", "92"),
    ]
    trade = _trade(side, rows, {0: [_entry(side, stop="93", limit="98")]})

    assert _flip(str(trade.entry_price), side) == Decimal("98")
    # Risk 5 (98 → 93). Best 103.5 is bar 1's close, worst 93 is the stop's fill on bar 3.
    assert _excursion(trade, side) == ("103.5", "93", "1.1", "1")


def test_a_trade_without_a_stop_has_prices_but_no_r() -> None:
    """Straight through the ledger: the sizer refuses an entry with no stop, but a live position
    can have none, and its excursions are still prices — only the R has no denominator."""
    portfolio = Portfolio(initial_capital=Decimal(100_000), instrument=AAPL)
    rows = _bars([*_OPEN_AND_HOLD, ("107", "110", "103", "105")])

    def fill(intent: SignalKind, price: str, index: int) -> Fill:
        order = OrderRequest(
            symbol="AAPL",
            side=Side.LONG,
            intent=intent,
            volume=Decimal(1),
            decided_at=rows[index - 1].time,
        )
        return Fill(
            order=order,
            time=rows[index].time,
            price=Decimal(price),
            volume=Decimal(1),
            costs=Decimal(0),
        )

    portfolio.apply(fill(SignalKind.ENTRY, "100", 1))
    portfolio.observe_bar(rows[1])
    portfolio.observe_bar(rows[2])
    portfolio.observe_price(Decimal(107))
    trade = portfolio.apply(fill(SignalKind.EXIT, "107", 3))

    assert trade is not None
    assert (trade.mfe_price, trade.mae_price) == (Decimal(108), Decimal(99))
    assert trade.mfe_r is None
    assert trade.mae_r is None


# --------------------------------------------------------------------------- #
# Invariants over random markets and every setup                               #
# --------------------------------------------------------------------------- #


@st.composite
def _random_walk(draw: st.DrawFn) -> list[Candle]:
    count = draw(st.integers(min_value=30, max_value=160))
    drift = draw(st.decimals(min_value="-0.8", max_value="0.8", places=1))
    step = st.decimals(min_value="-3", max_value="3", places=1)
    wick = st.decimals(min_value="0", max_value="1.5", places=1)
    # A gap between one close and the next open, sometimes: an exit at a gapped open is the one
    # fill that can land outside every range the position was held through.
    gap = st.sampled_from(["0", "0", "0", "-4", "4"])
    price, candles = Decimal(200), []
    for index in range(count):
        open_ = max(Decimal(20), price + Decimal(draw(gap)))
        close = max(Decimal(20), open_ + drift + draw(step))
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
        price = close
    return candles


@pytest.mark.parametrize(
    "kind", ["mme9_breakout", "mme9_turn", "mme9_pullback", "mme9_failed_turn", "structure_choch"]
)
@pytest.mark.parametrize("rr", [None, "2"])
@settings(max_examples=40, deadline=None)
@given(candles=_random_walk())
def test_every_trade_ends_inside_the_range_its_excursions_describe(
    kind: str, rr: str | None, candles: list[Candle]
) -> None:
    """Whatever the setup, the target or the market: both excursions are non-negative, the entry
    and the exit lie between the worst and the best price, and so the result in R lies between
    `-mae_r` and `mfe_r`. A trade that ended outside its own excursions would be a trade whose
    path was measured on the wrong bars."""
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=build_setup({"type": kind, "params": {"side": "both", "period": 3}})
        if kind.startswith("mme9")
        else build_setup({"type": kind, "params": {"side": "both"}}),
        broker=BacktestBroker(
            instrument=AAPL,
            initial_capital=Decimal(100_000),
            take_profit_rr=None if rr is None else Decimal(rr),
        ),
        risk=PercentRiskManager(percent=Decimal(1)),
    )
    for trade in result.trades:
        assert trade.mfe_price is not None
        assert trade.mae_price is not None
        low, high = sorted((trade.mfe_price, trade.mae_price))
        assert low <= trade.entry_price <= high
        assert low <= trade.exit_price <= high
        assert trade.mfe_r is not None
        assert trade.mae_r is not None
        assert trade.mfe_r >= 0
        assert trade.mae_r >= 0
        result_r = gross_r(trade)
        assert result_r is not None
        assert -trade.mae_r <= result_r <= trade.mfe_r


# --------------------------------------------------------------------------- #
# Deriving a target                                                             #
# --------------------------------------------------------------------------- #


def _closed(
    *,
    mfe_r: str | None,
    exit_price: str = "97",
    take_profit: str | None = None,
    side: Side = Side.LONG,
) -> ClosedTrade:
    """A long from 100 with its stop at 95 (risk 5) that left at `exit_price`."""
    return ClosedTrade(
        symbol="AAPL",
        side=side,
        volume=Decimal(1),
        entry_time=bar(0, open_="100", close="100").time,
        entry_price=Decimal(100),
        exit_time=bar(5, open_="100", close="100").time,
        exit_price=Decimal(exit_price),
        gross_pnl=Decimal(0),
        costs=Decimal(0),
        net_pnl=Decimal(0),
        stop_loss=Decimal(95) if side is Side.LONG else Decimal(105),
        take_profit=None if take_profit is None else Decimal(take_profit),
        mfe_r=None if mfe_r is None else Decimal(mfe_r),
    )


def test_a_target_the_trade_reached_is_what_it_would_have_made() -> None:
    trade = _closed(mfe_r="2.4")
    assert with_target(trade, Decimal(2)) == Decimal(2)
    assert with_target(trade, Decimal("2.4")) == Decimal("2.4")  # reached exactly counts


def test_a_target_the_trade_did_not_reach_leaves_it_as_it_ended() -> None:
    """Out at 97 from 100 on a risk of 5: -0.6 R, target or no target."""
    trade = _closed(mfe_r="2.4")
    assert with_target(trade, Decimal("2.5")) == Decimal("-0.6")


def test_the_result_is_gross_r_on_the_sell_side_too() -> None:
    trade = _closed(mfe_r="1", exit_price="103", side=Side.SHORT)
    assert gross_r(trade) == Decimal("-0.6")
    assert with_target(trade, Decimal(2)) == Decimal("-0.6")


def test_a_target_past_the_one_the_trade_had_cannot_be_derived() -> None:
    """It left at its own 2R target, so how far it would have gone after is not in the record."""
    trade = _closed(mfe_r="2", exit_price="110", take_profit="110")
    assert with_target(trade, Decimal(1)) == Decimal(1)
    assert with_target(trade, Decimal(2)) == Decimal(2)
    assert with_target(trade, Decimal(3)) is None


def test_an_unmeasured_trade_cannot_be_derived() -> None:
    assert with_target(_closed(mfe_r=None), Decimal(2)) is None


def test_a_target_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive multiple"):
        with_target(_closed(mfe_r="1"), Decimal(0))


# --------------------------------------------------------------------------- #
# The ladder a sweep run is scored at                                          #
# --------------------------------------------------------------------------- #


def _costed(
    *, mfe_r: str, exit_price: str, costs: str = "0", side: Side = Side.LONG
) -> ClosedTrade:
    """Two AAPL shares from 100 with the stop 5 away: a risk of 10 dollars, so two dollars are
    0.2 R. ⚠️ Two, not one: with one share a cost in R that forgot the volume would come out
    right by accident."""
    return dataclasses.replace(
        _closed(mfe_r=mfe_r, exit_price=exit_price, side=side),
        costs=Decimal(costs),
        volume=Decimal(2),
    )


def test_the_ladder_scores_every_rung_net_of_each_trades_costs() -> None:
    """Two trades. The first reached 2.4 R and left at 97 (-0.6 R); the second reached 0.8 R and
    left at 95 (-1 R). Each paid two dollars, 0.2 R of its risk.

    At 2 R the first is a hit (+2) and the second is not (-1): 1 R gross, 0.6 R net.
    At 3 R neither hits: -1.6 gross, -2 net."""
    trades = [
        _costed(mfe_r="2.4", exit_price="97", costs="2"),
        _costed(mfe_r="0.8", exit_price="95", costs="2"),
    ]
    ladder = target_ladder(trades, AAPL, (Decimal(2), Decimal(3)))

    two = ladder[Decimal(2)]
    assert two is not None
    assert (two.trades, two.hits, two.net_r, two.expectancy_r) == (
        2,
        1,
        Decimal("0.6"),
        Decimal("0.3"),
    )
    # The running sum goes +1.8 then -0.4: a fall of 1.2 from its peak.
    assert two.max_drawdown_r == Decimal("1.2")

    three = ladder[Decimal(3)]
    assert three is not None
    assert (three.hits, three.net_r) == (0, Decimal("-2.0"))
    # -0.8 then -2.0, never above the zero it started from: the whole fall is the drawdown.
    assert three.max_drawdown_r == Decimal("2.0")


def test_a_short_trades_costs_are_taken_off_in_r_of_its_own_risk() -> None:
    """Short from 100 with the stop at 105: the same 10 dollars of risk on two shares, above the
    entry this time. It reached 1 R and left at 103 (-0.6 R), paying 0.2 R in costs."""
    [trade] = [_costed(mfe_r="1", exit_price="103", costs="2", side=Side.SHORT)]
    ladder = target_ladder([trade], AAPL, (Decimal(1), Decimal(2)))

    one, two = ladder[Decimal(1)], ladder[Decimal(2)]
    assert one is not None
    assert two is not None
    assert (one.hits, one.net_r) == (1, Decimal("0.8"))
    assert (two.hits, two.net_r) == (0, Decimal("-0.8"))


def test_reaching_the_target_exactly_is_a_hit() -> None:
    [rung] = target_ladder([_costed(mfe_r="2", exit_price="97")], AAPL, (Decimal(2),)).values()
    assert rung is not None
    assert (rung.hits, rung.net_r) == (1, Decimal(2))


def test_a_rung_some_trade_cannot_answer_is_none_for_the_whole_run() -> None:
    """A sum over the trades that could answer would be a different set of trades at each rung."""
    trades = [_costed(mfe_r="2", exit_price="97"), _closed(mfe_r=None)]
    assert target_ladder(trades, AAPL, (Decimal(1),)) == {Decimal(1): None}


def test_a_run_with_no_trades_scores_zero_everywhere() -> None:
    [(rung, outcome)] = target_ladder([], AAPL, (Decimal(2),)).items()
    assert rung == Decimal(2)
    assert outcome is not None
    assert (outcome.trades, outcome.net_r, outcome.max_drawdown_r) == (0, Decimal(0), Decimal(0))


def test_the_ladder_is_his() -> None:
    assert [str(k) for k in LADDER] == [
        "0.5",
        "1",
        "1.5",
        "2",
        "2.5",
        "3",
        "4",
        "5",
        "6",
        "8",
        "10",
    ]
