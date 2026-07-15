"""The backtest broker, one bias at a time.

Most of these drive the broker directly — submit an order, hand it a bar — because that is
the only way to put a candle at exactly the shape a bias needs: a gap through a stop, a bar
that touches both stop and target, an entry whose own bar stops it out. A couple go through
the real `run()` to prove the loop's lookahead guard accepts what the broker produces.
"""

from decimal import Decimal

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import SpreadCostModel
from tradeforge_engine.domain import OrderRequest, Side, SignalKind
from tradeforge_engine.testing import EURUSD, START, bar

DECIDED = START  # a decision instant strictly before the first bar we hand the broker


def _broker(**kwargs: object) -> BacktestBroker:
    return BacktestBroker(instrument=EURUSD, initial_capital=Decimal(10_000), **kwargs)  # type: ignore[arg-type]


def _entry(
    broker: BacktestBroker,
    *,
    side: Side = Side.LONG,
    volume: str = "1",
    stop: str | None = None,
) -> None:
    broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=side,
            intent=SignalKind.ENTRY,
            volume=Decimal(volume),
            decided_at=DECIDED,
            stop_loss=Decimal(stop) if stop is not None else None,
        )
    )


def _exit(broker: BacktestBroker, *, side: Side, volume: str = "1") -> None:
    broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=side,
            intent=SignalKind.EXIT,
            volume=Decimal(volume),
            decided_at=DECIDED,
        )
    )


# --------------------------------------------------------------------------- #
# Construction and empty paths                                                  #
# --------------------------------------------------------------------------- #


def test_negative_slippage_and_non_positive_rr_are_refused() -> None:
    with pytest.raises(ValueError, match="slippage"):
        BacktestBroker(instrument=EURUSD, slippage_ticks=Decimal(-1))
    with pytest.raises(ValueError, match="R multiple"):
        BacktestBroker(instrument=EURUSD, take_profit_rr=Decimal(0))


def test_an_exit_with_no_position_open_does_nothing() -> None:
    """A strategy may emit an exit on a bar where the stop already closed the trade. The
    broker fills nothing rather than raise."""
    broker = _broker()
    _exit(broker, side=Side.LONG)
    assert (
        broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09900")) == []
    )


def test_an_entry_while_already_in_a_position_does_not_fill() -> None:
    """Phase 1 holds one position at a time; a second entry is refused rather than left to make
    the ledger raise mid-bar."""
    broker = _broker()
    _entry(broker, side=Side.LONG)  # no stop, so nothing closes it
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09900"))
    _entry(broker, side=Side.LONG)
    assert (
        broker.on_bar(bar(2, open_="1.10100", close="1.10200", high="1.10300", low="1.10000")) == []
    )
    assert broker.positions("EURUSD")  # the original position is untouched


# --------------------------------------------------------------------------- #
# Strategy-condition exits (filled at the open, not at a protective level)       #
# --------------------------------------------------------------------------- #


def test_a_condition_exit_closes_a_long_at_the_open() -> None:
    broker = _broker(slippage_ticks=Decimal(2))
    _entry(broker, side=Side.LONG)  # no stop: only a condition exit can close it
    broker.on_bar(bar(1, open_="1.10000", close="1.10200", high="1.10300", low="1.09900"))
    _exit(broker, side=Side.LONG)
    [fill] = broker.on_bar(bar(2, open_="1.10500", close="1.10600", high="1.10700", low="1.10400"))
    assert fill.order.intent is SignalKind.EXIT
    assert fill.price == Decimal("1.10498")  # selling: open minus two ticks of slippage


def test_a_condition_exit_closes_a_short_at_the_open() -> None:
    broker = _broker(slippage_ticks=Decimal(2))
    _entry(broker, side=Side.SHORT)
    broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09700"))
    _exit(broker, side=Side.SHORT)
    [fill] = broker.on_bar(bar(2, open_="1.09500", close="1.09400", high="1.09600", low="1.09300"))
    assert fill.price == Decimal("1.09502")  # buying to close: open plus two ticks


# --------------------------------------------------------------------------- #
# Fill timing and price                                                         #
# --------------------------------------------------------------------------- #


def test_a_pending_order_fills_at_the_next_bars_open() -> None:
    broker = _broker()
    _entry(broker)
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.10200", high="1.10300", low="1.09900"))
    assert fill.order.intent is SignalKind.ENTRY
    assert fill.price == Decimal("1.10000")  # the open, no slippage configured


def test_slippage_moves_the_fill_against_you() -> None:
    """A buy fills a little higher, a sell a little lower — two ticks of 0.00001 here."""
    long_broker = _broker(slippage_ticks=Decimal(2))
    _entry(long_broker, side=Side.LONG)
    [long_fill] = long_broker.on_bar(
        bar(1, open_="1.10000", close="1.10200", high="1.10300", low="1.09900")
    )
    assert long_fill.price == Decimal("1.10002")

    short_broker = _broker(slippage_ticks=Decimal(2))
    _entry(short_broker, side=Side.SHORT)
    [short_fill] = short_broker.on_bar(
        bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09700")
    )
    assert short_fill.price == Decimal("1.09998")


def test_slippage_is_clamped_to_the_bar() -> None:
    """You cannot be filled above the high or below the low — nobody traded there, and the
    loop's range guard would rightly reject it. A bar that opens at its high slips no further."""
    broker = _broker(slippage_ticks=Decimal(10))
    _entry(broker, side=Side.LONG)
    # open == high: a long cannot slip above it
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09900", high="1.10000", low="1.09800"))
    assert fill.price == Decimal("1.10000")


def test_entry_and_exit_both_pay_the_cost() -> None:
    broker = _broker(
        cost_model=SpreadCostModel(spread_points=Decimal(10)), take_profit_rr=Decimal(2)
    )
    _entry(broker, stop="1.09000")  # distance 0.01, target 1.10000 + 0.02 = 1.12000
    [entry] = broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    assert entry.costs == Decimal("5")  # half of a 10-point spread on 1 lot
    # a bar that reaches the target
    [exit_] = broker.on_bar(bar(2, open_="1.11500", close="1.12500", high="1.12500", low="1.11400"))
    assert exit_.order.reason == "tp"
    assert exit_.costs == Decimal("5")


# --------------------------------------------------------------------------- #
# Protective exits: stop, target, worst case, gap                               #
# --------------------------------------------------------------------------- #


def test_a_long_is_stopped_out_at_its_stop() -> None:
    broker = _broker()
    _entry(broker, stop="1.09500")
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    # next bar dips to the stop
    [fill] = broker.on_bar(bar(2, open_="1.09800", close="1.09600", high="1.09850", low="1.09400"))
    assert fill.order.reason == "sl"
    assert fill.price == Decimal("1.09500")  # filled at the stop level


def test_a_long_takes_profit_at_its_target() -> None:
    broker = _broker(take_profit_rr=Decimal(2))
    _entry(broker, stop="1.09500")  # entry 1.10000, risk 0.005, target 1.10000 + 0.01 = 1.11000
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    [fill] = broker.on_bar(bar(2, open_="1.10500", close="1.11200", high="1.11300", low="1.10400"))
    assert fill.order.reason == "tp"
    assert fill.price == Decimal("1.11000")


def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop() -> None:
    """The most expensive fantasy: 'the stop always fills at the stop'. Price gaps below it at
    the open; the fill is the open, worse than the stop — the loss the market actually gave."""
    broker = _broker()
    _entry(broker, stop="1.09500")
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    # gaps open below the stop
    [fill] = broker.on_bar(bar(2, open_="1.09000", close="1.08800", high="1.09100", low="1.08700"))
    assert fill.order.reason == "sl"
    assert fill.price == Decimal("1.09000")  # the open, not 1.09500


def test_stop_and_target_in_one_bar_takes_the_stop_worst_case() -> None:
    """When a single bar's range covers both levels, the tick order is unknowable — so the
    backtest assumes the stop. The optimistic call is how a strategy invents an edge."""
    broker = _broker(take_profit_rr=Decimal(2))
    _entry(broker, stop="1.09500")  # entry 1.10000, target 1.11000
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    # this bar's range covers both 1.09500 (stop) and 1.11000 (target)
    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.10000", high="1.11200", low="1.09400"))
    assert fill.order.reason == "sl"
    assert fill.price == Decimal("1.09500")


def test_a_protective_exit_fills_at_the_level_without_slippage() -> None:
    """A documented v1 simplification (engine-guardian, PR-105): the stop and target fill at
    their exact level, with no adverse slippage of their own — even when the broker is
    configured with slippage on open-based fills. A real stop is a market order and would
    slip; here the gap-through-stop rule (`min(open, stop)`) is the only pessimism modelled on
    protective exits. This test pins the choice so a future change to it is deliberate."""
    broker = _broker(slippage_ticks=Decimal(5))  # slippage IS configured
    _entry(broker, stop="1.09500")
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    [fill] = broker.on_bar(bar(2, open_="1.09800", close="1.09600", high="1.09850", low="1.09400"))
    assert fill.order.reason == "sl"
    assert fill.price == Decimal("1.09500")  # the level exactly, not 1.09495


def test_a_short_is_stopped_out_above_and_targets_below() -> None:
    stop_broker = _broker(take_profit_rr=Decimal(2))
    _entry(stop_broker, side=Side.SHORT, stop="1.10500")  # entry 1.10000, risk 0.005
    stop_broker.on_bar(bar(1, open_="1.10000", close="1.09900", high="1.10050", low="1.09800"))
    [stop_fill] = stop_broker.on_bar(
        bar(2, open_="1.10200", close="1.10600", high="1.10700", low="1.10100")
    )
    assert stop_fill.order.reason == "sl"
    assert stop_fill.price == Decimal("1.10500")

    tp_broker = _broker(take_profit_rr=Decimal(2))
    _entry(tp_broker, side=Side.SHORT, stop="1.10500")  # target 1.10000 - 0.01 = 1.09000
    tp_broker.on_bar(bar(1, open_="1.10000", close="1.09900", high="1.10050", low="1.09800"))
    [tp_fill] = tp_broker.on_bar(
        bar(2, open_="1.09500", close="1.08900", high="1.09600", low="1.08800")
    )
    assert tp_fill.order.reason == "tp"
    assert tp_fill.price == Decimal("1.09000")
