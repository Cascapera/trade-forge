"""The backtest broker, one bias at a time.

Most of these drive the broker directly — submit an order, hand it a bar — because that is
the only way to put a candle at exactly the shape a bias needs: a gap through a stop, a bar
that touches both stop and target, an entry whose own bar stops it out. A couple go through
the real `run()` to prove the loop's lookahead guard accepts what the broker produces.
"""

import datetime as dt
import logging
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import SpreadCostModel
from tradeforge_engine.domain import (
    Context,
    OrderRequest,
    OrderResult,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError, LookaheadError
from tradeforge_engine.loop import _reject_lookahead, run
from tradeforge_engine.testing import EURUSD, HOUR, START, FixedRisk, ScriptedStrategy, bar

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


def _limit(  # noqa: PLR0913 — keyword-only; each names one axis of a resting order
    broker: BacktestBroker,
    *,
    limit: str,
    side: Side = Side.LONG,
    volume: str = "1",
    stop: str | None = None,
    client_id: str = "zone-1",
    decided_at: dt.datetime = DECIDED,
) -> OrderResult:
    return broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=side,
            intent=SignalKind.ENTRY,
            volume=Decimal(volume),
            decided_at=decided_at,
            stop_loss=Decimal(stop) if stop is not None else None,
            limit_price=Decimal(limit),
            client_id=client_id,
        )
    )


def _stop(  # noqa: PLR0913 — keyword-only; each names one axis of a resting order
    broker: BacktestBroker,
    *,
    stop_price: str,
    side: Side = Side.LONG,
    volume: str = "1",
    stop: str | None = None,
    client_id: str = "zone-1",
    decided_at: dt.datetime = DECIDED,
) -> OrderResult:
    return broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=side,
            intent=SignalKind.ENTRY,
            volume=Decimal(volume),
            decided_at=decided_at,
            stop_loss=Decimal(stop) if stop is not None else None,
            stop_price=Decimal(stop_price),
            client_id=client_id,
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


# --------------------------------------------------------------------------- #
# Limit orders: resting, filling at a level, and being withdrawn (ADR-0014)     #
# --------------------------------------------------------------------------- #


def test_a_buy_limit_fills_at_its_level_when_the_bar_trades_down_to_it() -> None:
    """The golden case, and the reason the whole feature exists: the structure setups enter at
    the edge of a region. The bar opens at 1.10000 and dips to 1.09400; the order placed at
    1.09500 fills *there*, not at the open five hundred points worse."""
    broker = _broker()
    _limit(broker, limit="1.09500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    assert fill.price == Decimal("1.09500")
    assert fill.order.client_id == "zone-1"


def test_a_sell_limit_fills_at_its_level_when_the_bar_trades_up_to_it() -> None:
    broker = _broker()
    _limit(broker, side=Side.SHORT, limit="1.10500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.10200", high="1.10600", low="1.09900"))
    assert fill.price == Decimal("1.10500")


def test_a_bar_that_opens_beyond_the_limit_fills_at_the_open() -> None:
    """A limit is "this price or better". Price gaps down through a buy limit at 1.09500 and
    opens at 1.09000 — the fill is the open, because that is what the market offered. The
    mirrored case (a sell limit gapped through) fills at its open too, for the same reason."""
    long_broker = _broker()
    _limit(long_broker, limit="1.09500")
    [long_fill] = long_broker.on_bar(
        bar(1, open_="1.09000", close="1.08900", high="1.09100", low="1.08800")
    )
    assert long_fill.price == Decimal("1.09000")

    short_broker = _broker()
    _limit(short_broker, side=Side.SHORT, limit="1.10500")
    [short_fill] = short_broker.on_bar(
        bar(1, open_="1.11000", close="1.11100", high="1.11200", low="1.10900")
    )
    assert short_fill.price == Decimal("1.11000")


def test_a_bar_that_never_reaches_the_limit_leaves_it_resting() -> None:
    """The order outlives the bar — that is the difference between a limit and everything else
    in this broker. The second bar reaches it, and it fills at the level."""
    broker = _broker()
    _limit(broker, limit="1.09500")
    quiet = bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09600")
    assert broker.on_bar(quiet) == []
    [fill] = broker.on_bar(bar(2, open_="1.09800", close="1.09400", high="1.09850", low="1.09300"))
    assert fill.price == Decimal("1.09500")

    # the mirror: a sell limit the bar's high never reaches
    short_broker = _broker()
    _limit(short_broker, side=Side.SHORT, limit="1.10500")
    short_quiet = bar(1, open_="1.10000", close="1.10200", high="1.10400", low="1.09900")
    assert short_broker.on_bar(short_quiet) == []


def test_a_resting_order_cannot_fill_on_the_bar_that_placed_it() -> None:
    """The anti-lookahead rule where it can actually be broken.

    A limit is the one fill priced *inside* a bar, so "fill at the next open" is no longer
    doing the work — only the `decided_at` comparison is. The order is decided on the bar at
    index 1, and that same bar's range covers the level: no fill. Delete the guard in
    `_fill_resting` and this test fails on the first assertion, which is the point of it.
    """
    broker = _broker()
    deciding = bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400")
    _limit(broker, limit="1.09500", decided_at=deciding.time)
    assert broker.on_bar(deciding) == []
    # the next bar covers the same level, and now the order is eligible
    [fill] = broker.on_bar(bar(2, open_="1.09800", close="1.09600", high="1.09900", low="1.09400"))
    assert fill.price == Decimal("1.09500")


def test_cancelling_a_resting_order_stops_it_from_ever_filling() -> None:
    """The order's lifetime belongs to the strategy: its zone was mitigated, so it withdraws
    the order. The bar that would have filled it fills nothing."""
    broker = _broker()
    _limit(broker, limit="1.09500")
    assert broker.cancel("zone-1") is True
    reaching = bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400")
    assert broker.on_bar(reaching) == []


def test_cancelling_an_unknown_order_is_false_not_an_error() -> None:
    """In live this is a race, not a bug: the venue fills while the cancel is in flight. A
    broker that raised would turn a normal execution into a dead session."""
    broker = _broker()
    assert broker.cancel("never-existed") is False
    _limit(broker, limit="1.09500")
    broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    assert broker.cancel("zone-1") is False  # already filled


def test_a_second_order_with_the_same_client_id_is_refused() -> None:
    """Two orders answering to one name make `cancel` a question with two answers."""
    broker = _broker()
    assert _limit(broker, limit="1.09500").accepted is True
    duplicate = _limit(broker, limit="1.09000")
    assert duplicate.accepted is False
    assert "already resting" in duplicate.reason
    # and the refusal is real: only the first order is waiting
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.08900", high="1.10100", low="1.08800"))
    assert fill.price == Decimal("1.09500")


def test_a_resting_order_without_a_name_is_refused() -> None:
    broker = _broker()
    result = broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=Side.LONG,
            intent=SignalKind.ENTRY,
            volume=Decimal(1),
            decided_at=DECIDED,
            limit_price=Decimal("1.09500"),
        )
    )
    assert result.accepted is False
    assert "client_id" in result.reason
    reaching = bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400")
    assert broker.on_bar(reaching) == []


def test_an_exit_cannot_rest_at_a_limit() -> None:
    """A resting exit would be a second take-profit beside the protective one, and two paths
    closing one position is where the ledger stops adding up."""
    broker = _broker()
    _entry(broker, side=Side.LONG)
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09900"))
    result = broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=Side.LONG,
            intent=SignalKind.EXIT,
            volume=Decimal(1),
            decided_at=DECIDED,
            limit_price=Decimal("1.11000"),
            client_id="target-1",
        )
    )
    assert result.accepted is False
    assert "only an entry" in result.reason


def test_a_limit_does_not_fill_while_a_position_is_open_and_keeps_waiting() -> None:
    """Phase 1 holds one position. The order is not cancelled — a venue would not withdraw it,
    and only the strategy knows whether it still makes sense — so it fills once the position
    is out of the way."""
    broker = _broker()
    _entry(broker, side=Side.LONG)  # no stop: nothing closes it but a condition exit
    _limit(broker, limit="1.09500")
    # bar 1: the market entry fills at the open, and the same bar trades through the resting
    # level — which must not fill, because the position is open the whole way down
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    assert fill.order.limit_price is None
    # bar 2 closes the position; the limit is still waiting, untouched by any of it
    _exit(broker, side=Side.LONG)
    broker.on_bar(bar(2, open_="1.09800", close="1.09900", high="1.10000", low="1.09750"))
    # bar 3 reaches the level again, flat this time, and now it fills
    [late] = broker.on_bar(bar(3, open_="1.09900", close="1.09400", high="1.09950", low="1.09300"))
    assert late.price == Decimal("1.09500")


def test_a_limit_fill_pays_no_slippage() -> None:
    """A limit order is a promise of "this price or better". Adverse slippage would fill it
    worse than the level, which is the one thing it cannot do. Pinned so that changing it
    later is a decision rather than an accident."""
    broker = _broker(slippage_ticks=Decimal(10))
    _limit(broker, limit="1.09500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    assert fill.price == Decimal("1.09500")


def test_a_limit_entry_can_be_stopped_out_on_its_own_bar() -> None:
    """Same rule as a market entry (step 3): the stop was decided when the order was placed, so
    its exit inherits that instant and the bar that filled the entry may also stop it.

    The returned pair is sorted exit-first, as `on_bar` sorts every bar's fills — a reversal
    has to close before it opens, and that ordering is the ledger's, not the clock's."""
    broker = _broker()
    _limit(broker, limit="1.09500", stop="1.09200")
    exit_fill, entry_fill = broker.on_bar(
        bar(1, open_="1.10000", close="1.09150", high="1.10100", low="1.09100")
    )
    assert entry_fill.price == Decimal("1.09500")
    assert exit_fill.order.reason == "sl"
    assert exit_fill.price == Decimal("1.09200")
    assert broker.positions("EURUSD") == ()


def test_a_limit_entry_stopped_by_a_wick_that_closes_back_above_it() -> None:
    """The most common way a limit entry dies, and the other half of the asymmetry.

    On the entry bar the target needs the close as proof, and the stop does **not** — and this
    is the bar that says why. Price fills the limit at 1.09500, wicks to 1.09250 through the
    stop at 1.09300, and closes back up at 1.09800. Nothing about the close proves anything
    here, yet the stop was unquestionably hit: the entry happened on the way down, so the
    bar's low can only have printed at or after it.

    Demanding proof of the close on the stop too — the tidy-looking symmetry — would turn
    every wick-out into a position carried happily into the next bar. It is the failure this
    engine exists to refuse, wearing the costume of consistency."""
    broker = _broker()
    _limit(broker, limit="1.09500", stop="1.09300")
    exit_fill, entry_fill = broker.on_bar(
        bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09250")
    )
    assert entry_fill.price == Decimal("1.09500")
    assert exit_fill.order.reason == "sl"
    assert exit_fill.price == Decimal("1.09300")  # the level, on a bar that closed above it
    assert broker.positions("EURUSD") == ()

    # the mirror: a sell limit wicked through its stop, closing back below it
    selling = _broker()
    _limit(selling, side=Side.SHORT, limit="1.10500", stop="1.10700")
    sell_exit, sell_entry = selling.on_bar(
        bar(1, open_="1.10000", close="1.10200", high="1.10750", low="1.09900")
    )
    assert sell_entry.price == Decimal("1.10500")
    assert sell_exit.order.reason == "sl"
    assert sell_exit.price == Decimal("1.10700")


def test_only_one_resting_order_fills_per_bar_and_the_rest_keep_waiting() -> None:
    """Two levels reachable on one bar, one position allowed. Submission order decides — the
    tie has to be broken by something deterministic, and arrival is the only fact available."""
    broker = _broker()
    _limit(broker, limit="1.09500", client_id="zone-a")
    _limit(broker, limit="1.09300", client_id="zone-b")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09250", high="1.10100", low="1.09200"))
    assert fill.order.client_id == "zone-a"
    assert broker.cancel("zone-b") is True  # still resting, not silently dropped


# --------------------------------------------------------------------------- #
# Limit orders: what the bar that fills them is NOT allowed to also do          #
# --------------------------------------------------------------------------- #


def test_a_limit_entry_takes_profit_on_its_own_bar_only_when_the_close_proves_it() -> None:
    """On the bar that fills a limit, the target needs *proof*, and the close is the proof.

    The high of that bar may have printed before the entry existed — a buy limit fills on the
    way down — so "the high reached the target" shows nothing. The close does: price walked
    from the fill to the close, so a close beyond the target crossed it after the entry,
    necessarily. Entry 1.09500, stop 1.09300, 2R ⇒ target 1.09900; this bar closes at 1.09950.

    And it fills at the level exactly, with no gap treatment: the bar's open (1.10000) is a
    price from before this position existed, so it cannot be where it exited. Pricing the exit
    there books 2.5R on a 2R trade — the same inflation whether it happens on this bar or is
    deferred to the next one."""
    broker = _broker(take_profit_rr=Decimal(2))
    _limit(broker, limit="1.09500", stop="1.09300")
    exit_fill, entry_fill = broker.on_bar(  # exit-first, as `on_bar` sorts every bar's fills
        bar(1, open_="1.10000", close="1.09950", high="1.10100", low="1.09400")
    )
    assert entry_fill.price == Decimal("1.09500")
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("1.09900")
    assert broker.positions("EURUSD") == ()


def test_the_newborn_rules_mirror_for_a_sell_limit() -> None:
    """The short side of both halves. Sell limit at 1.10500, stop 1.10700 ⇒ 200 points of risk,
    2R ⇒ target 1.10100. The first bar closes at 1.10050, below the target, which proves the
    cross happened after the fill; the second broker's bar reaches the stop instead."""
    winner = _broker(take_profit_rr=Decimal(2))
    _limit(winner, side=Side.SHORT, limit="1.10500", stop="1.10700")
    exit_fill, entry_fill = winner.on_bar(
        bar(1, open_="1.10000", close="1.10050", high="1.10600", low="1.09900")
    )
    assert entry_fill.price == Decimal("1.10500")
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("1.10100")

    loser = _broker(take_profit_rr=Decimal(2))
    _limit(loser, side=Side.SHORT, limit="1.10500", stop="1.10700")
    stopped_exit, stopped_entry = loser.on_bar(
        bar(1, open_="1.10000", close="1.10750", high="1.10800", low="1.09900")
    )
    assert stopped_entry.price == Decimal("1.10500")
    assert stopped_exit.order.reason == "sl"
    assert stopped_exit.price == Decimal("1.10700")  # the level, not the 1.10000 open


def test_a_newborn_bar_covering_both_levels_takes_the_stop_too() -> None:
    """The worst-case rule, on the path this PR added. A carried position already has
    `test_stop_and_target_in_one_bar_takes_the_stop_worst_case`; the newborn reading is new
    code and needs its own, because the two `if`s in `_newborn_protective_price` can be
    swapped without a single other test noticing.

    A reversal bar around a release: buy limit 1.09500 with its stop at 1.09300 ⇒ 200 points
    of risk, 2R ⇒ target 1.09900. Price opens at 1.10000, sells off through the limit **and**
    through the stop (low 1.09250), then closes at 1.09950 — beyond the target. Both readings
    are available, so the bar resolves against the trade: -1R, not +2R. Reading it the other
    way is 3R of invented edge per occurrence, on exactly the news bars these zones fill on."""
    broker = _broker(take_profit_rr=Decimal(2))
    _limit(broker, limit="1.09500", stop="1.09300")
    exit_fill, entry_fill = broker.on_bar(
        bar(1, open_="1.10000", close="1.09950", high="1.10050", low="1.09250")
    )
    assert entry_fill.price == Decimal("1.09500")
    assert exit_fill.order.reason == "sl"
    assert exit_fill.price == Decimal("1.09300")

    # the mirror: a sell limit whose bar spikes through the stop and closes past the target
    selling = _broker(take_profit_rr=Decimal(2))
    _limit(selling, side=Side.SHORT, limit="1.09500", stop="1.09700")  # target 1.09100
    sell_exit, sell_entry = selling.on_bar(
        bar(1, open_="1.09000", close="1.09050", high="1.09750", low="1.08950")
    )
    assert sell_entry.price == Decimal("1.09500")
    assert sell_exit.order.reason == "sl"
    assert sell_exit.price == Decimal("1.09700")


def test_a_target_the_close_falls_short_of_is_left_for_the_next_bar() -> None:
    """The residue, and the one case that stays genuinely unknowable: this bar's high tags the
    target at 1.10100 but it closes at 1.09600, below it. The tag may have come before the
    entry, so the position carries into the next bar — where it existed at the open again and
    the ordinary reading applies. The next bar is a normal one (it opens where the last
    closed), and the target fills at its level."""
    broker = _broker(take_profit_rr=Decimal(2))
    _limit(broker, limit="1.09500", stop="1.09300")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09600", high="1.10100", low="1.09400"))
    assert fill.order.intent is SignalKind.ENTRY
    assert broker.positions("EURUSD")  # nothing proved, nothing claimed

    [exit_fill] = broker.on_bar(
        bar(2, open_="1.09600", close="1.09950", high="1.10000", low="1.09550")
    )
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("1.09900")

    # the mirror: a sell limit whose bar dips to the target but closes above it
    selling = _broker(take_profit_rr=Decimal(2))
    _limit(selling, side=Side.SHORT, limit="1.10500", stop="1.10700")  # target 1.10100
    [sell_fill] = selling.on_bar(
        bar(1, open_="1.10200", close="1.10400", high="1.10600", low="1.10050")
    )
    assert sell_fill.order.intent is SignalKind.ENTRY
    assert selling.positions("EURUSD")


def test_a_deferred_target_is_not_paid_at_the_next_bars_open() -> None:
    """The trap that a first fix walked into: withholding the target on the entry bar and
    letting the next bar apply gap treatment turns a deferral into a windfall. The position
    would cross into a bar that opens beyond the target and be paid at *that* open — a gap the
    engine invented by deferring, not one the market printed.

    Here the entry bar closes at 1.09950, past the 1.09900 target, so the target is settled on
    that bar at its level. Nothing is left to be repriced against the next open at 1.10000."""
    broker = _broker(take_profit_rr=Decimal(2))
    _limit(broker, limit="1.09500", stop="1.09300")
    fills = broker.on_bar(bar(1, open_="1.10000", close="1.09950", high="1.10100", low="1.09400"))
    assert [fill.price for fill in fills] == [Decimal("1.09900"), Decimal("1.09500")]  # exit, entry
    assert (
        broker.on_bar(bar(2, open_="1.10000", close="1.10100", high="1.10200", low="1.09950")) == []
    )


def test_a_carried_position_is_paid_at_the_open_when_the_market_really_gaps() -> None:
    """The other half of the same rule, so neither can drift: a position that existed at the
    open *does* get the better price when the market gaps through its target. A take-profit is
    a sell limit, and a market that opens beyond it fills you there — the same mechanism that
    fills a resting entry at the open when price gaps through its level.

    Entry at the open of bar 1 (1.10000), stop 1.09000, 2R ⇒ target 1.12000. Bar 2 opens at
    1.12500, already past it."""
    broker = _broker(take_profit_rr=Decimal(2))
    _entry(broker, stop="1.09000")
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    [exit_fill] = broker.on_bar(
        bar(2, open_="1.12500", close="1.12600", high="1.12700", low="1.12400")
    )
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("1.12500")  # the open, better than the 1.12000 target

    # the mirror: a short whose market gaps down through its target
    short_broker = _broker(take_profit_rr=Decimal(2))
    _entry(short_broker, side=Side.SHORT, stop="1.11000")  # entry 1.10000, target 1.08000
    short_broker.on_bar(bar(1, open_="1.10000", close="1.09900", high="1.10050", low="1.09800"))
    [short_exit] = short_broker.on_bar(
        bar(2, open_="1.07500", close="1.07400", high="1.07600", low="1.07300")
    )
    assert short_exit.order.reason == "tp"
    assert short_exit.price == Decimal("1.07500")  # the open, better than the 1.08000 target


def test_a_limit_does_not_fill_on_a_bar_that_closed_another_position() -> None:
    """A bar that stopped a position out at 1.09000 also traded 1.09500 on the way down. Filling
    a buy limit at 1.09500 off that range books an entry at a price that only existed while the
    other position was open — 500 points of entry improvement, invented. The tick order that
    would settle it does not exist, so the ambiguous bar resolves against the trade."""
    broker = _broker()
    _entry(broker, side=Side.LONG, stop="1.09000")
    broker.on_bar(bar(1, open_="1.10000", close="1.09900", high="1.10100", low="1.09850"))
    _limit(broker, limit="1.09500")
    [fill] = broker.on_bar(bar(2, open_="1.09900", close="1.08600", high="1.10050", low="1.08500"))
    assert fill.order.reason == "sl"  # the stop, and nothing else
    assert [order.client_id for order in broker.resting()] == ["zone-1"]  # still waiting


def test_a_limit_fills_on_a_bar_whose_position_left_at_the_open() -> None:
    """A trade that ended at the open ended on the first tick, and the rest of that bar was
    demonstrably flat — there is no tick order to be ambiguous about. This is the method's
    canonical reversal: close the runner at the open, and the same bar comes back to the next
    zone. Blocking it would delete the trade in silence and leave the order to fill at some
    different level bars later."""
    broker = _broker()
    _entry(broker, side=Side.LONG)
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    _exit(broker, side=Side.LONG)  # a condition exit: fills at the next open
    _limit(broker, limit="1.09500")
    exit_fill, entry_fill = broker.on_bar(
        bar(2, open_="1.10000", close="1.09600", high="1.10050", low="1.09400")
    )
    assert exit_fill.price == Decimal("1.10000")  # left at the open
    assert entry_fill.price == Decimal("1.09500")  # so the limit was free to fill


def test_a_limit_fills_when_a_stop_gapped_out_at_the_open_too() -> None:
    """Same rule through the protective path: price gapped below the stop, so the exit *is*
    the open and the position was gone from the first tick."""
    broker = _broker()
    _entry(broker, side=Side.LONG, stop="1.09800")
    broker.on_bar(bar(1, open_="1.10000", close="1.10100", high="1.10200", low="1.09950"))
    _limit(broker, limit="1.09500")
    exit_fill, entry_fill = broker.on_bar(
        bar(2, open_="1.09700", close="1.09450", high="1.09750", low="1.09400")
    )
    assert exit_fill.order.reason == "sl"
    assert exit_fill.price == Decimal("1.09700")  # the open, below the 1.09800 stop
    assert entry_fill.price == Decimal("1.09500")


def test_a_limit_fills_when_a_market_entry_gapped_straight_through_its_stop() -> None:
    """The third way a bar can be free after the open, and the one that is easiest to argue
    away: the market entry fills at the open **and** its stop, gapped through overnight, fills
    at that same open. The position began and ended on the first tick.

    The pair below is the whole argument — identical candle, identical economics, and the only
    difference is which bar the position was born on. Blocking one and not the other would
    make the answer depend on a fact the market cannot see."""
    gap_bar = {"open_": "1.09500", "close": "1.09150", "high": "1.09550", "low": "1.09100"}

    born_here = _broker()
    _entry(born_here, side=Side.LONG, stop="1.09800")  # decided when price was up at 1.10000
    _limit(born_here, limit="1.09200")
    fills = born_here.on_bar(bar(1, **gap_bar))
    # the market entry and its stop both land on the open, and then the limit is free to fill
    assert [(fill.order.reason, fill.price) for fill in fills] == [
        ("sl", Decimal("1.09500")),
        ("", Decimal("1.09500")),
        ("", Decimal("1.09200")),
    ]
    assert born_here.positions("EURUSD")

    born_earlier = _broker()
    _entry(born_earlier, side=Side.LONG, stop="1.09800")
    born_earlier.on_bar(bar(1, open_="1.10000", close="1.10000", high="1.10050", low="1.09950"))
    _limit(born_earlier, limit="1.09200")
    later = born_earlier.on_bar(bar(2, **gap_bar))
    # same candle, same answer — the limit fills at the same price either way
    assert [(fill.order.reason, fill.price) for fill in later] == [
        ("sl", Decimal("1.09500")),
        ("", Decimal("1.09200")),
    ]


def test_a_limit_does_not_fill_on_a_bar_that_stopped_a_newly_opened_position() -> None:
    """The third way a bar can be occupied, and the only one with no test of its own: the
    position was *born* on this bar, at the open, and died at its stop **inside** it.

    The three tests above all prove the permissive direction — a position that left *at* the
    open frees the rest of the bar. This one proves the blocking direction on the same path,
    which is the half that costs money if it goes missing: the market entry fills at 1.10000,
    is stopped at 1.09600 partway down, and the buy limit at 1.09400 must stay resting even
    though the bar traded through it. Price only reached 1.09400 while the other position was
    open, and the tick order that would say otherwise does not exist."""
    broker = _broker()
    _entry(broker, side=Side.LONG, stop="1.09600")
    _limit(broker, limit="1.09400")
    fills = broker.on_bar(bar(1, open_="1.10000", close="1.09500", high="1.10050", low="1.09300"))
    assert [(fill.order.reason, fill.price) for fill in fills] == [
        ("sl", Decimal("1.09600")),  # inside the bar, not at the 1.10000 open
        ("", Decimal("1.10000")),
    ]
    assert [order.client_id for order in broker.resting()] == ["zone-1"]  # still waiting


def test_an_order_the_market_gapped_past_its_own_stop_is_dropped() -> None:
    """Buy limit at 1.09500 with its stop at 1.09300; price opens at 1.09200. Filling would
    open a position already below its own exit — a scratch trade whose only content is the
    spread. The level it was waiting at is behind the market now, so the order is dropped
    rather than left resting."""
    broker = _broker()
    _limit(broker, limit="1.09500", stop="1.09300")
    assert (
        broker.on_bar(bar(1, open_="1.09200", close="1.09100", high="1.09250", low="1.09000")) == []
    )
    assert broker.resting() == ()
    assert broker.positions("EURUSD") == ()


def test_a_fill_exactly_at_its_own_stop_is_dropped_too() -> None:
    """The boundary the drop rule created: a fill *at* the stop, not past it. The position
    would open and close on the same bar for nothing — the scratch trade the rule exists to
    remove — so the frontier is `price > stop`, strictly."""
    broker = _broker()
    _limit(broker, limit="1.09500", stop="1.09300")
    assert (
        broker.on_bar(bar(1, open_="1.09300", close="1.09250", high="1.09350", low="1.09200")) == []
    )
    assert broker.resting() == ()


def test_a_dead_order_does_not_cost_the_next_one_its_fill() -> None:
    """Dropping an order says nothing about the one behind it in the queue. If the drop ended
    the bar, one dead order would silently swallow another order's fill — and the backtest
    would be missing a trade with nothing to point at."""
    broker = _broker()
    _limit(broker, limit="1.09500", stop="1.09300", client_id="dead")  # gapped past its stop
    _limit(broker, limit="1.09400", stop="1.08000", client_id="alive")
    [fill] = broker.on_bar(bar(1, open_="1.09200", close="1.09150", high="1.09250", low="1.09100"))
    assert fill.order.client_id == "alive"
    assert fill.price == Decimal("1.09200")  # the open: it gapped through this level too
    assert broker.resting() == ()


def test_the_target_is_measured_from_the_fill_not_from_the_level() -> None:
    """A bar that opens beyond the limit fills better than the level, and the whole point of a
    risk multiple is that it is measured from the price actually paid. Fill 1.09000 with a stop
    at 1.08800 is 200 points of risk ⇒ a 2R target at 1.09400. Measured from the level instead
    (1.09500 - 1.08800 = 700), the target would be 1.10900 and the trade would look like it
    needed a move three times bigger to pay the same 2R."""
    broker = _broker(take_profit_rr=Decimal(2))
    _limit(broker, limit="1.09500", stop="1.08800")
    [entry_fill] = broker.on_bar(
        bar(1, open_="1.09000", close="1.09100", high="1.09150", low="1.08950")
    )
    assert entry_fill.price == Decimal("1.09000")
    [exit_fill] = broker.on_bar(
        bar(2, open_="1.09200", close="1.09450", high="1.09500", low="1.09150")
    )
    assert exit_fill.order.reason == "tp"
    assert exit_fill.price == Decimal("1.09400")


def test_a_resting_fill_pays_the_entry_cost() -> None:
    """Costs are plugged in, and the new fill path is not exempt (AGENTS.md §5.6): half of a
    10-point spread on one lot is $5, the same as any other entry."""
    broker = _broker(cost_model=SpreadCostModel(spread_points=Decimal(10)))
    _limit(broker, limit="1.09500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    assert fill.costs == Decimal("5")


def test_the_level_itself_counts_as_reached() -> None:
    """The residual optimism ADR-0014 accepts, pinned so it cannot drift in either direction: a
    bar whose low is *exactly* the level fills, and so does one that opens there and never
    moves. In a real book you might have been behind the queue; without tick data there is no
    way to know, and the level being a price the strategy chose is what keeps it honest."""
    touching = _broker()
    _limit(touching, limit="1.09500")
    [fill] = touching.on_bar(
        bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09500")
    )
    assert fill.price == Decimal("1.09500")

    flat = _broker()
    _limit(flat, limit="1.09500")
    [flat_fill] = flat.on_bar(
        bar(1, open_="1.09500", close="1.09500", high="1.09500", low="1.09500")
    )
    assert flat_fill.price == Decimal("1.09500")

    # the mirror: a sell limit whose level is exactly this bar's high
    selling = _broker()
    _limit(selling, side=Side.SHORT, limit="1.10500")
    [sell_fill] = selling.on_bar(
        bar(1, open_="1.10000", close="1.10200", high="1.10500", low="1.09900")
    )
    assert sell_fill.price == Decimal("1.10500")

    # and one tick short of the level is not reached
    missed = _broker()
    _limit(missed, limit="1.09500")
    assert (
        missed.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09501")) == []
    )


def test_a_name_that_has_filled_cannot_be_reused() -> None:
    """A strategy that re-emits its zone's signal every bar would otherwise place a second
    order under the same name while the first one's position is open — invisible, unreachable
    by `cancel`, and able to fill much later off a zone that stopped existing."""
    broker = _broker()
    _limit(broker, limit="1.09500")
    broker.on_bar(bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"))
    again = _limit(broker, limit="1.09500")
    assert again.accepted is False
    assert "already filled" in again.reason


def test_a_limit_on_the_wrong_side_of_the_market_is_refused() -> None:
    """A buy limit rests below the market and a sell limit above; the wrong side is a sign
    error, not an exotic order. It would not announce itself — it simply fills at the next
    open, sized against a price that never existed — and the structure layer computes these
    levels from zone edges, which is exactly where a top/bottom swap happens."""
    with pytest.raises(ValueError, match="wrong side"):
        Signal(
            kind=SignalKind.ENTRY,
            side=Side.LONG,
            reference_price=Decimal("1.10000"),
            limit_price=Decimal("1.10500"),
            client_id="zone-1",
        )
    with pytest.raises(ValueError, match="wrong side"):
        Signal(
            kind=SignalKind.ENTRY,
            side=Side.SHORT,
            reference_price=Decimal("1.10000"),
            limit_price=Decimal("1.09500"),
            client_id="zone-1",
        )


def test_a_limit_entry_survives_the_loops_guards_end_to_end() -> None:
    """Through the real `run()`, because the fill this feature adds is priced inside a bar and
    the loop's guards (decided-before, inside-this-bar, inside-the-range) are what make that
    safe. A broker test alone would never ask them."""
    strategy = ScriptedStrategy(
        script={
            1: [
                Signal(
                    kind=SignalKind.ENTRY,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09000"),
                    limit_price=Decimal("1.09500"),
                    client_id="zone-1",
                )
            ]
        }
    )
    candles = [
        bar(0, open_="1.10000", close="1.10000"),
        bar(1, open_="1.10000", close="1.10000"),
        # the decision bar is index 1; this one dips to the level
        bar(2, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"),
    ]
    broker = _broker()
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=broker,
        risk=FixedRisk(),
    )
    [fill] = result.fills
    assert fill.time == candles[2].time
    assert fill.order.decided_at == candles[1].time
    assert fill.price == Decimal("1.09500")


def test_the_loop_says_out_loud_when_the_broker_refuses_an_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refusal that only the broker knows about looks exactly like a trade that never
    triggered — the one failure a strategy author cannot debug from the results."""
    signal = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09000"),
        limit_price=Decimal("1.09500"),
        client_id="zone-1",
        reason="same-zone-twice",
    )
    with caplog.at_level(logging.DEBUG, logger="tradeforge_engine.loop"):
        run(
            candles=[bar(index, open_="1.10000", close="1.10000") for index in range(4)],
            timeframe=HOUR,
            instrument=EURUSD,
            # the same order, twice: the second one is refused as a duplicate name
            strategy=ScriptedStrategy(script={1: [signal], 2: [signal]}),
            broker=_broker(),
            risk=FixedRisk(),
        )
    assert "broker refused same-zone-twice" in caplog.text
    assert "already resting" in caplog.text


def test_an_exit_carrying_a_limit_price_is_reported_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exits fill at the open; the broker's protective levels are the only thing that closes a
    position at a price. An exit that quietly became a market order is a strategy measuring
    something other than what it asked for, so the loop says so."""
    strategy = ScriptedStrategy(
        script={
            1: [Signal(kind=SignalKind.ENTRY, side=Side.LONG, reference_price=Decimal("1.10000"))],
            2: [
                Signal(
                    kind=SignalKind.EXIT,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    limit_price=Decimal("1.11000"),
                )
            ],
        }
    )
    with caplog.at_level(logging.DEBUG, logger="tradeforge_engine.loop"):
        result = run(
            candles=[bar(index, open_="1.10000", close="1.10000") for index in range(5)],
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=strategy,
            broker=_broker(),
            risk=FixedRisk(),
        )
    assert "exits fill at the open" in caplog.text
    # and it did close, at the open, rather than resting at 1.11000
    [exit_fill] = [fill for fill in result.fills if fill.order.intent is SignalKind.EXIT]
    assert exit_fill.price == Decimal("1.10000")


def test_a_cancel_signal_withdraws_the_order_through_the_loop() -> None:
    """The strategy's zone died before price came back. `SignalKind.CANCEL` is the only intent
    that never becomes an order, so the loop has to route it around sizing and the veto."""
    strategy = ScriptedStrategy(
        script={
            1: [
                Signal(
                    kind=SignalKind.ENTRY,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09000"),
                    limit_price=Decimal("1.09500"),
                    client_id="zone-1",
                )
            ],
            2: [
                Signal(
                    kind=SignalKind.CANCEL,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    client_id="zone-1",
                )
            ],
        }
    )
    candles = [
        bar(0, open_="1.10000", close="1.10000"),
        bar(1, open_="1.10000", close="1.10000"),
        bar(2, open_="1.10000", close="1.10000"),
        # would have filled, had the cancel not landed on the bar before
        bar(3, open_="1.10000", close="1.09800", high="1.10100", low="1.09400"),
    ]
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=_broker(),
        risk=FixedRisk(),
    )
    assert result.fills == ()


# --------------------------------------------------------------------------- #
# Stop entry orders: filling on the break of a level (ADR-0016)                 #
# --------------------------------------------------------------------------- #


def test_a_buy_stop_fills_at_its_level_when_the_bar_breaks_up_to_it() -> None:
    """The golden case, and the reason the feature exists: the swing setups enter on the break of
    a level. A buy stop rests *above* the market at 1.10500; the bar opens at 1.10000 and runs up
    to 1.10600, so the order fills at 1.10500 — the mirror of a sell limit, but a long."""
    broker = _broker()
    _stop(broker, stop_price="1.10500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.10550", high="1.10600", low="1.09900"))
    assert fill.price == Decimal("1.10500")
    assert fill.order.side is Side.LONG
    assert fill.order.client_id == "zone-1"


def test_a_sell_stop_fills_at_its_level_when_the_bar_breaks_down_to_it() -> None:
    broker = _broker()
    _stop(broker, side=Side.SHORT, stop_price="1.09500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.09450", high="1.10100", low="1.09400"))
    assert fill.price == Decimal("1.09500")
    assert fill.order.side is Side.SHORT


def test_a_bar_that_opens_beyond_the_stop_fills_at_the_open_the_worse_price() -> None:
    """The decision at the heart of ADR-0016, and the one thing that is *not* a mirror of the
    limit: a stop is "this price or worse". Price gaps up through a buy stop at 1.10500 and opens
    at 1.11000 — the fill is the open, 500 points *worse* than the trigger, because a stop is a
    market order the gap already triggered. The mirrored sell stop, gapped down, fills at its
    worse open too."""
    long_broker = _broker()
    _stop(long_broker, stop_price="1.10500")
    [long_fill] = long_broker.on_bar(
        bar(1, open_="1.11000", close="1.11100", high="1.11200", low="1.10950")
    )
    assert long_fill.price == Decimal("1.11000")

    short_broker = _broker()
    _stop(short_broker, side=Side.SHORT, stop_price="1.09500")
    [short_fill] = short_broker.on_bar(
        bar(1, open_="1.09000", close="1.08900", high="1.09100", low="1.08800")
    )
    assert short_fill.price == Decimal("1.09000")


def test_a_bar_that_never_reaches_the_stop_leaves_it_resting() -> None:
    """The order outlives the bar. A buy stop at 1.10500 with the bar's high only 1.10400 rests;
    the next bar breaks the level and it fills there."""
    broker = _broker()
    _stop(broker, stop_price="1.10500")
    quiet = bar(1, open_="1.10000", close="1.10200", high="1.10400", low="1.09900")
    assert broker.on_bar(quiet) == []
    [fill] = broker.on_bar(bar(2, open_="1.10200", close="1.10600", high="1.10700", low="1.10100"))
    assert fill.price == Decimal("1.10500")

    # the mirror: a sell stop the bar's low never reaches
    short_broker = _broker()
    _stop(short_broker, side=Side.SHORT, stop_price="1.09500")
    short_quiet = bar(1, open_="1.10000", close="1.09800", high="1.10100", low="1.09600")
    assert short_broker.on_bar(short_quiet) == []


def test_a_resting_stop_cannot_fill_on_the_bar_that_placed_it() -> None:
    """The anti-lookahead rule where it can actually be broken, for the stop path. The order is
    decided on the bar at index 1, whose range already breaks the level: no fill. Delete the
    `decided_at` guard in `_fill_resting` and this fails on the first assertion."""
    broker = _broker()
    deciding = bar(1, open_="1.10000", close="1.10550", high="1.10600", low="1.09900")
    _stop(broker, stop_price="1.10500", decided_at=deciding.time)
    assert broker.on_bar(deciding) == []
    # the next bar breaks the same level, and now the order is eligible
    [fill] = broker.on_bar(bar(2, open_="1.10200", close="1.10600", high="1.10700", low="1.10100"))
    assert fill.price == Decimal("1.10500")


def test_the_stop_level_itself_counts_as_reached() -> None:
    """The residual optimism ADR-0016 accepts, mirrored from the limit and pinned so it cannot
    drift: a bar whose high is *exactly* the trigger fills, and one tick short does not."""
    touching = _broker()
    _stop(touching, stop_price="1.10500")
    [fill] = touching.on_bar(
        bar(1, open_="1.10000", close="1.10400", high="1.10500", low="1.09900")
    )
    assert fill.price == Decimal("1.10500")

    missed = _broker()
    _stop(missed, stop_price="1.10500")
    assert (
        missed.on_bar(bar(1, open_="1.10000", close="1.10400", high="1.10499", low="1.09900")) == []
    )


def test_a_stop_entry_can_be_stopped_out_on_its_own_bar() -> None:
    """A stop entry is born mid-bar like a limit, so the newborn protective reading has to hold
    for it too. A buy stop at 1.10500 with its loss at 1.10000 fills as the bar breaks up, and
    the same bar sells off to 1.09900 — the stop closes it, on the bar it opened."""
    broker = _broker()
    _stop(broker, stop_price="1.10500", stop="1.10000")
    fills = broker.on_bar(bar(1, open_="1.10200", close="1.10050", high="1.10600", low="1.09900"))
    reasons = [fill.order.reason for fill in fills]
    assert "sl" in reasons
    [stop_fill] = [fill for fill in fills if fill.order.reason == "sl"]
    assert stop_fill.price == Decimal("1.10000")


def test_a_stop_entry_books_a_loss_on_a_bar_that_closed_above_it() -> None:
    """The house rule, in the shape that costs a breakout the most — pinned with its whole P&L
    because it looks wrong until you see why it is not.

    A buy stop at 1.10500 with its loss at 1.10300, on a bar that opens at 1.10200, prints its
    low at 1.10100, and **closes at 1.10580 — above the entry**. The engine books entry 1.10500,
    exit 1.10300, -200: a loss on a bar that finished in the trade's favour.

    That low sits *behind* where the break came from, so it may well have printed before the
    entry existed — the bar is genuinely ambiguous, and no tick data settles it. The engine
    resolves ambiguity against the trade everywhere else, and does so here too. Read the
    alternative out loud and it is worse: an engine that decides its own doubts in its favour
    reports breakout results nobody can trade.
    """
    broker = _broker()
    _stop(broker, stop_price="1.10500", stop="1.10300")
    fills = broker.on_bar(bar(1, open_="1.10200", close="1.10580", high="1.10600", low="1.10100"))
    assert [(fill.order.intent, fill.order.reason, fill.price) for fill in fills] == [
        (SignalKind.EXIT, "sl", Decimal("1.10300")),
        (SignalKind.ENTRY, "", Decimal("1.10500")),
    ]
    [trade] = broker.trades()
    assert trade.entry_price == Decimal("1.10500")
    assert trade.exit_price == Decimal("1.10300")
    assert trade.net_pnl == Decimal(-200)


def test_a_stop_that_gapped_to_the_open_owns_its_whole_bar() -> None:
    """A resting order that filled *at the open* was not born inside anything: the bar gapped
    through the level and the order executed on the first tick, so the whole bar belongs to the
    position and the ordinary protective reading applies — the target off the **high**, not the
    close. Hard-coding the newborn reading here denied it a target its bar demonstrably reached.

    The proof is the market order beside it: same entry price, same bar, same protective levels,
    so the two must book the same trade. They now do.
    """
    gapped = _broker(take_profit_rr=Decimal(2))
    # trigger 1.10500, loss 1.09500: risk 1000, so the fill may slip 500 and this one slips 300
    _stop(gapped, stop_price="1.10500", stop="1.09500")
    breakout = bar(1, open_="1.10800", close="1.13000", high="1.13500", low="1.10700")
    gapped_fills = gapped.on_bar(breakout)

    at_market = _broker(take_profit_rr=Decimal(2))
    _entry(at_market, stop="1.09500")
    market_fills = at_market.on_bar(breakout)

    assert [(fill.order.reason, fill.price) for fill in gapped_fills] == [
        (fill.order.reason, fill.price) for fill in market_fills
    ]
    # the entry is the open, and the target — 2R off the *realised* entry — fills off the high
    assert [(fill.order.reason, fill.price) for fill in gapped_fills] == [
        ("tp", Decimal("1.13400")),
        ("", Decimal("1.10800")),
    ]


def test_a_stop_filled_inside_the_bar_still_reads_its_target_from_the_close() -> None:
    """The other half of the pair above: this one really was born mid-bar, so the newborn rule
    holds. Same levels, but the bar opens *below* the trigger and breaks up to it — the high tags
    the target and the close falls short, and the position carries instead of booking a target
    the high may have printed before the entry existed."""
    broker = _broker(take_profit_rr=Decimal(2))
    _stop(broker, stop_price="1.10500", stop="1.09500")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.12000", high="1.13500", low="1.09900"))
    assert fill.price == Decimal("1.10500")
    assert broker.trades() == ()


def test_a_stop_on_the_wrong_side_of_the_market_is_refused() -> None:
    """A buy stop rests *above* the market and a sell stop *below* — the mirror of the limit, and
    the worse sign error: a buy stop below the market is already triggered, so it would fill at
    the next open as a silent market order sized against a level price never had to break."""
    with pytest.raises(ValueError, match="wrong side"):
        Signal(
            kind=SignalKind.ENTRY,
            side=Side.LONG,
            reference_price=Decimal("1.10000"),
            stop_price=Decimal("1.09500"),
            client_id="zone-1",
        )
    with pytest.raises(ValueError, match="wrong side"):
        Signal(
            kind=SignalKind.ENTRY,
            side=Side.SHORT,
            reference_price=Decimal("1.10000"),
            stop_price=Decimal("1.10500"),
            client_id="zone-1",
        )


def test_an_order_cannot_carry_both_a_limit_and_a_stop() -> None:
    """The two say opposite things about which side the order rests on; a price meaning both is a
    bug, refused at the boundary rather than resolved by picking one."""
    with pytest.raises(ValueError, match="limit or a stop"):
        Signal(
            kind=SignalKind.ENTRY,
            side=Side.LONG,
            reference_price=Decimal("1.10000"),
            limit_price=Decimal("1.09500"),
            stop_price=Decimal("1.10500"),
            client_id="zone-1",
        )


def test_a_stop_entry_survives_the_loops_guards_end_to_end() -> None:
    """Through the real `run()`, because a stop fill is priced inside a bar and the loop's guards
    (decided-before, inside-this-bar, inside-the-range) are what make that safe."""
    strategy = ScriptedStrategy(
        script={
            1: [
                Signal(
                    kind=SignalKind.ENTRY,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09500"),
                    stop_price=Decimal("1.10500"),
                    client_id="zone-1",
                )
            ]
        }
    )
    candles = [
        bar(0, open_="1.10000", close="1.10000"),
        bar(1, open_="1.10000", close="1.10000"),
        # the decision bar is index 1; this one breaks up to the level
        bar(2, open_="1.10000", close="1.10550", high="1.10600", low="1.09900"),
    ]
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=_broker(),
        risk=FixedRisk(),
    )
    [fill] = result.fills
    assert fill.time == candles[2].time
    assert fill.order.decided_at == candles[1].time
    assert fill.price == Decimal("1.10500")


# --------------------------------------------------------------------------- #
# The slip ceiling: a stop fill too far past its trigger (ADR-0016)            #
# --------------------------------------------------------------------------- #


def test_a_stop_that_gaps_far_past_its_trigger_is_dropped() -> None:
    """The mirror of `_survives_the_gap`, for the mirror order.

    A buy stop at 1.10500 with its loss at 1.10000 was sized for 500 points of risk. The market
    gaps and opens at 1.15000: filling there opens a position risking 5000 — **ten times** what
    the risk manager agreed to, from an order it never got to re-size. One overnight gap in an
    index would book a 10% loss in an account that promised 1%.

    So the order is dropped, on the same terms as a limit whose gap carried it past its own
    stop: the level the strategy chose is behind the market, and the trade it priced is gone.
    """
    broker = _broker()
    _stop(broker, stop_price="1.10500", stop="1.10000")
    assert (
        broker.on_bar(bar(1, open_="1.15000", close="1.15100", high="1.15200", low="1.14900")) == []
    )
    assert broker.resting() == ()  # dropped, not left waiting


def test_a_sell_stop_that_gaps_far_past_its_trigger_is_dropped() -> None:
    broker = _broker()
    _stop(broker, side=Side.SHORT, stop_price="1.09500", stop="1.10000")
    assert (
        broker.on_bar(bar(1, open_="1.05000", close="1.04900", high="1.05100", low="1.04800")) == []
    )
    assert broker.resting() == ()


def test_a_stop_fills_when_the_gap_stays_inside_the_ceiling() -> None:
    """The other side of the line, so the guard cannot quietly become "no gap ever fills". The
    same order gapping to 1.10700 slips 200 points against an allowance of 250 — it fills, at
    the open, worse than its trigger, exactly as ADR-0016 says."""
    broker = _broker()
    _stop(broker, stop_price="1.10500", stop="1.10000")
    [fill] = broker.on_bar(bar(1, open_="1.10700", close="1.10800", high="1.10900", low="1.10650"))
    assert fill.price == Decimal("1.10700")


def test_a_stop_whose_turn_comes_late_is_dropped_rather_than_filled_at_the_market() -> None:
    """The same ceiling catching the case with **no gap anywhere** — an engine artefact rather
    than a market event, and the one a backtest would never suspect.

    A stop order rests while another position holds the slot (step 4 of `on_bar` waits for it).
    Price breaks the trigger on a bar the order is not eligible for, and by the time the slot
    frees the market is 1500 points past it. Without the ceiling the order fills *there* — a
    market entry at a price the strategy never chose, sized against a trigger far behind.
    """
    broker = _broker()
    _entry(broker, stop="1.09000")  # occupies the one position slot
    _stop(broker, stop_price="1.10500", stop="1.10000", client_id="zone-2")

    broker.on_bar(bar(1, open_="1.10000", close="1.10050", high="1.10100", low="1.09900"))
    # breaks the trigger, but the slot is taken: nothing fills
    assert (
        broker.on_bar(bar(2, open_="1.10100", close="1.10700", high="1.10800", low="1.10050")) == []
    )

    _exit(broker, side=Side.LONG)
    fills = broker.on_bar(bar(3, open_="1.12000", close="1.12100", high="1.12200", low="1.11900"))
    assert [fill.order.intent for fill in fills] == [SignalKind.EXIT]
    assert broker.resting() == ()


def test_a_stop_without_a_loss_has_no_ceiling_to_measure() -> None:
    """Nothing sized this order against a distance, so there is no risk to blow through: it
    fills wherever the gap left it. The guard measures a promise; with no stop there is none."""
    broker = _broker()
    _stop(broker, stop_price="1.10500")
    [fill] = broker.on_bar(bar(1, open_="1.15000", close="1.15100", high="1.15200", low="1.14900"))
    assert fill.price == Decimal("1.15000")


# --------------------------------------------------------------------------- #
# The resting lifecycle, exercised through a stop order                        #
# --------------------------------------------------------------------------- #


def test_a_resting_stop_can_be_cancelled_by_name() -> None:
    broker = _broker()
    _stop(broker, stop_price="1.10500", client_id="break-1")
    assert broker.cancel("break-1") is True
    assert (
        broker.on_bar(bar(1, open_="1.10000", close="1.10600", high="1.10700", low="1.09900")) == []
    )


def test_two_stops_cannot_share_a_name() -> None:
    broker = _broker()
    _stop(broker, stop_price="1.10500", client_id="break-1")
    rejected = _stop(broker, stop_price="1.10600", client_id="break-1")
    assert rejected.accepted is False
    assert "already resting" in rejected.reason


def test_a_stops_name_is_spent_once_it_has_filled() -> None:
    broker = _broker()
    _stop(broker, stop_price="1.10500", client_id="break-1")
    broker.on_bar(bar(1, open_="1.10000", close="1.10600", high="1.10700", low="1.09900"))
    rejected = _stop(broker, stop_price="1.10500", client_id="break-1")
    assert rejected.accepted is False
    assert "already filled" in rejected.reason


def test_a_limit_and_a_stop_resting_together_fill_in_arrival_order() -> None:
    """Both order types share one queue, and one bar can reach both levels. Phase 1 holds one
    position, so the tie is broken by arrival — the limit was submitted first, so it fills and
    the stop stays resting, untouched, for a later bar."""
    broker = _broker()
    _limit(broker, limit="1.09800", client_id="pullback")
    _stop(broker, stop_price="1.10500", client_id="breakout")
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.10550", high="1.10600", low="1.09700"))
    assert fill.order.client_id == "pullback"
    assert fill.price == Decimal("1.09800")
    assert [order.client_id for order in broker.resting()] == ["breakout"]


@given(
    prices=st.lists(
        st.decimals(min_value="1.00000", max_value="1.20000", places=5), min_size=5, max_size=5
    ),
    long=st.booleans(),
)
def test_a_stop_never_fills_better_than_its_trigger(prices: list[Decimal], long: bool) -> None:
    """The half of the promise the docstring makes and no example could pin: *no favourable
    slippage*. A stop is "this price or worse", so however the bar is shaped, a buy stop fills
    at or above its trigger and a sell stop at or below — and always inside the bar, which is
    what keeps the loop's range guard from ever firing.

    Property-based because the interesting shapes are the ones nobody thinks to write: the bar
    that opens exactly on the trigger, the doji, the bar that gaps and reverses.
    """
    open_, close, trigger, *rest = prices
    high = max(open_, close, *rest)
    low = min(open_, close, *rest)
    side = Side.LONG if long else Side.SHORT

    broker = _broker()
    _stop(broker, side=side, stop_price=str(trigger))
    candle = bar(1, open_=str(open_), close=str(close), high=str(high), low=str(low))
    fills = broker.on_bar(candle)
    if not fills:
        return
    [fill] = fills
    assert candle.low <= fill.price <= candle.high
    assert fill.price >= trigger if long else fill.price <= trigger


def test_the_ceiling_is_reached_but_not_crossed_at_exactly_half_the_risk() -> None:
    """The boundary, pinned on both sides, because a ceiling nobody tested at its own edge is a
    number that drifts. Trigger 1.10500 with its loss at 1.10000 allows 250 points of slip: a
    fill at 1.10750 spends exactly half the risk and still trades; one tick further does not."""
    at_the_line = _broker()
    _stop(at_the_line, stop_price="1.10500", stop="1.10000")
    [fill] = at_the_line.on_bar(
        bar(1, open_="1.10750", close="1.10800", high="1.10900", low="1.10700")
    )
    assert fill.price == Decimal("1.10750")

    one_tick_past = _broker()
    _stop(one_tick_past, stop_price="1.10500", stop="1.10000")
    assert (
        one_tick_past.on_bar(
            bar(1, open_="1.10751", close="1.10800", high="1.10900", low="1.10700")
        )
        == []
    )


def test_a_stop_whose_loss_sits_on_its_trigger_has_no_risk_to_measure() -> None:
    """Zero sized risk means the ceiling has nothing to be a fraction *of* — half of nothing
    would reject every fill, silently, for an order the strategy did place. The risk manager
    would have zeroed this lot before it ever reached a broker, but the broker is also driven
    directly (half this suite does), so the degenerate case answers for itself here."""
    broker = _broker()
    _stop(broker, stop_price="1.10500", stop="1.10500")
    [fill] = broker.on_bar(bar(1, open_="1.10600", close="1.10700", high="1.10800", low="1.10550"))
    assert fill.price == Decimal("1.10600")


# --------------------------------------------------------------------------- #
# Moving the stop of an open position (ADR-0018)                               #
# --------------------------------------------------------------------------- #

BAR_ONE = START + HOUR  # the bar the modifications below are decided on


def _open_a_long(broker: BacktestBroker, *, stop: str | None = "1.09000") -> None:
    """Fill a market long at 1.10000 on bar 1, so bar 2 onwards can test the stop."""
    _entry(broker, side=Side.LONG, stop=stop)
    [fill] = broker.on_bar(bar(1, open_="1.10000", close="1.10000"))
    assert fill.price == Decimal("1.10000")


def test_a_tightened_stop_is_where_the_position_is_stopped_out() -> None:
    """The golden. Bar 2 dips to 1.09400 — past the new stop, nowhere near the original one.

    Without the modification this bar does nothing at all: 1.09400 never reaches 1.09000. So a
    single assertion covers three separate ways to break this — the modification not applying,
    applying at the wrong level, and the exit still reading the old one.
    """
    broker = _broker()
    _open_a_long(broker)

    assert broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE) is True

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.09400", low="1.09400"))
    assert fill.price == Decimal("1.09500")
    assert fill.order.reason == "sl"


def test_the_modified_stop_carries_the_instant_it_was_decided_not_the_entrys() -> None:
    """The whole ADR in one assertion, and the one that fails if anyone collapses the two
    instants back into one.

    A stop the strategy moved on bar 1 was decided on bar 1 — not when the entry was. Wearing
    the entry's older stamp, the exit would clear `loop._reject_lookahead` on *any* bar,
    including the one whose close decided the level. Nothing about the resulting equity curve
    would look wrong; it would simply be better than the market gave.
    """
    broker = _broker()
    _open_a_long(broker)
    broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE)

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.09400", low="1.09400"))

    assert fill.order.decided_at == BAR_ONE
    assert fill.order.decided_at != DECIDED  # the entry's — the stamp that hides the bug


def test_the_guard_bites_on_a_modified_stop_filling_inside_its_own_bar() -> None:
    """The other half of the test above: an honest stamp is only worth having because the
    engine checks it. A stop decided on bar 1, exiting *inside* bar 1, is refused.

    This one calls the guard directly instead of driving a misbehaving broker through `run()`,
    the way `test_lookahead.py` does — and the reason is the point being made. With the loop as
    written, **no broker can produce this fill**: `modify_stop` is called after `on_bar` has
    already returned, so the next protective check always happens on a later candle. The
    ordering makes the bug unreachable today, which is exactly why the guard, and not the
    ordering, has to be what forbids it. Reorder those two steps, or hand the protocol to an
    `MT5Broker` that reconciles differently, and this is the line still standing.
    """
    broker = _broker()
    _open_a_long(broker)
    broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE)
    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.09400", low="1.09400"))

    with pytest.raises(LookaheadError):
        _reject_lookahead(fill, bar(1, open_="1.10000", close="1.09400", low="1.09400"), HOUR)


def test_a_long_stop_may_not_be_moved_away_from_price() -> None:
    """The lot was sized against 1.09000. Widening to 1.08000 doubles the money at risk on a
    position nobody re-sized — martingale wearing a trailing stop's clothes."""
    broker = _broker()
    _open_a_long(broker)

    with pytest.raises(EngineError, match="may only tighten"):
        broker.modify_stop("EURUSD", Decimal("1.08000"), BAR_ONE)


def test_a_short_stop_may_not_be_moved_away_from_price() -> None:
    """The mirror, and a separate test because the comparison flips: a short's stop sits
    *above* it, so loosening means moving up. One `<` left unflipped and only this fails."""
    broker = _broker()
    _entry(broker, side=Side.SHORT, stop="1.11000")
    broker.on_bar(bar(1, open_="1.10000", close="1.10000"))

    with pytest.raises(EngineError, match="may only tighten"):
        broker.modify_stop("EURUSD", Decimal("1.12000"), BAR_ONE)


def test_a_short_stop_tightens_downward() -> None:
    """And the short's tightening direction, which the refusal above does not prove."""
    broker = _broker()
    _entry(broker, side=Side.SHORT, stop="1.11000")
    broker.on_bar(bar(1, open_="1.10000", close="1.10000"))

    assert broker.modify_stop("EURUSD", Decimal("1.10500"), BAR_ONE) is True

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.10600", high="1.10600"))
    assert fill.price == Decimal("1.10500")
    assert fill.order.reason == "sl"


def test_moving_a_stop_to_where_it_already_is_is_accepted() -> None:
    """A strategy that recomputes the same level every bar must not be punished for it, and
    the author's own conduction does exactly that: while price holds above the average, the
    stop stays on the bar that broke it and the setup keeps naming that same low."""
    broker = _broker()
    _open_a_long(broker)

    assert broker.modify_stop("EURUSD", Decimal("1.09000"), BAR_ONE) is True


def test_modifying_with_no_position_open_is_false_not_an_error() -> None:
    """In live the trade may have closed while the instruction was in flight. That is a race,
    and a broker that raised would turn a normal execution into a dead session."""
    assert _broker().modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE) is False


def test_modifying_another_symbols_position_is_false() -> None:
    broker = _broker()
    _open_a_long(broker)

    assert broker.modify_stop("GBPUSD", Decimal("1.09500"), BAR_ONE) is False


def test_a_stop_can_be_armed_on_a_position_that_had_none() -> None:
    """An entry with no `stop_loss` leaves no protection at all. Arming one later only ever
    reduces risk, and refusing it would be the engine declining to make a position safer.

    The stamp is asserted here too, and it is not decoration: this branch builds a
    `_Protection` from scratch rather than replacing one, so it is the one path where the
    decision instant could be filled in from anywhere at all and every other test would still
    pass. A mutation run proved exactly that — the level was checked, the instant was not.
    """
    broker = _broker()
    _open_a_long(broker, stop=None)

    # Proof it really was unprotected: this bar would have taken any stop at 1.09500.
    assert broker.on_bar(bar(2, open_="1.10000", close="1.09400", low="1.09400")) == []

    armed_on = START + 2 * HOUR
    assert broker.modify_stop("EURUSD", Decimal("1.09500"), armed_on) is True

    [fill] = broker.on_bar(bar(3, open_="1.09600", close="1.09400", low="1.09400"))
    assert fill.price == Decimal("1.09500")
    assert fill.order.reason == "sl"
    assert fill.order.decided_at == armed_on


def test_the_target_keeps_its_level_and_its_own_instant_across_a_stop_modification() -> None:
    """`modify_stop` touches the stop and nothing else. The take-profit was computed from the
    entry price at the fill and was decided when the entry was — so it keeps both its level and
    the entry's stamp, which is the whole reason the two instants are stored separately."""
    broker = _broker(take_profit_rr=Decimal(2))
    _open_a_long(broker)  # entry 1.10000, stop 1.09000 -> risk 0.01000, target 1.12000
    broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE)

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.12100", high="1.12100"))

    assert fill.price == Decimal("1.12000")
    assert fill.order.reason == "tp"
    assert fill.order.decided_at == DECIDED  # the entry's, unmoved


def test_a_stop_can_be_tightened_more_than_once() -> None:
    """The author's conduction ratchets: each bar that closes back across the average brings
    the stop to that bar's extreme, and the level before it is never revisited."""
    broker = _broker()
    _open_a_long(broker)
    assert broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE) is True
    assert broker.modify_stop("EURUSD", Decimal("1.10000"), START + 2 * HOUR) is True

    with pytest.raises(EngineError, match="may only tighten"):
        broker.modify_stop("EURUSD", Decimal("1.09900"), START + 3 * HOUR)

    [fill] = broker.on_bar(bar(3, open_="1.10200", close="1.09900", low="1.09900"))
    assert fill.price == Decimal("1.10000")


def test_a_modification_moves_where_a_real_run_exits() -> None:
    """End to end through `run()`: the strategy asks on bar 2, the loop stamps and routes, and
    the trade exits at the tightened level on bar 3 — a level the original stop never reaches.

    This is also what proves the intent never becomes an order: a `MODIFY_STOP` that fell
    through to `_to_order` would be sized, queued and filled as a *second* entry, and the run
    would end holding a position instead of flat.

    **The modification deliberately does not happen on the bar the entry filled on.** Three
    instants are in play — the entry's decision (`DECIDED`), the entry's fill (`BAR_ONE`, which
    is `position.entry_time`), and the bar that moved the stop — and the exit must carry the
    third. Ask on bar 1 and all three collapse onto two, so stamping the modification with
    `position.entry_time` instead of `candle.time` passes: the assertion pins a *value* while
    leaving the *source* free. That substitution is the one ADR-0018 calls "the bug", and it is
    what `Broker.modify_stop` promises does not happen — so the whole promise rides on this bar
    being a different bar.
    """
    broker = _broker()
    modified_on = START + 2 * HOUR
    strategy = ScriptedStrategy(
        script={
            0: [
                Signal(
                    kind=SignalKind.ENTRY,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09000"),
                )
            ],
            2: [
                Signal(
                    kind=SignalKind.MODIFY_STOP,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09500"),
                )
            ],
        }
    )
    result = run(
        candles=[
            bar(0, open_="1.10000", close="1.10000"),
            bar(1, open_="1.10000", close="1.10000"),  # the entry fills here: entry_time
            bar(2, open_="1.10000", close="1.10000"),  # the stop moves here: decided_at
            bar(3, open_="1.10000", close="1.09400", low="1.09400"),
        ],
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=broker,
        risk=FixedRisk(),
    )

    [entry_fill, exit_fill] = result.fills
    assert entry_fill.order.intent is SignalKind.ENTRY
    assert entry_fill.time == BAR_ONE
    assert exit_fill.price == Decimal("1.09500")
    assert exit_fill.order.reason == "sl"
    assert exit_fill.order.decided_at == modified_on
    assert broker.positions("EURUSD") == ()


def test_an_untouched_stop_exits_carrying_the_entrys_instant() -> None:
    """The stamp on the path every stopped-out trade in every backtest already took.

    `_arm_protection` fills `stop_decided_at` from the entry, and until a modification arrives
    that is the truth: nothing has moved the stop. It reads as too obvious to test — which is
    exactly why a mutation run put **1970** there and all 488 tests stayed green. The level was
    right, the exit was right, the P&L was right; only the field the whole ADR exists to
    protect was wrong, and wrong in the direction that goes quiet (a stamp far enough in the
    past can never trip `_reject_lookahead`, while one in the future trips it at once).

    Its sibling on the arm-from-scratch branch is asserted in
    `test_a_stop_can_be_armed_on_a_position_that_had_none`. This is the busy one.
    """
    broker = _broker()
    _open_a_long(broker)

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.08900", low="1.08900"))

    assert fill.price == Decimal("1.09000")
    assert fill.order.reason == "sl"
    assert fill.order.decided_at == DECIDED


def test_arming_a_stop_on_an_unstopped_position_invents_no_target() -> None:
    """Arming protection where there was none creates a stop and *only* a stop.

    The take-profit is computed from the entry price at the fill, and that moment has gone; a
    target conjured here would close positions in the one kind of setup this method was built
    for — the MME9 conduction runs with `take_profit_rr=None` on purpose, because the stop is
    the only way out. A phantom target would report that method finishing every trade at a tidy
    +1R: consistent, positive, and not the method at all.

    **The probe bar's high is absurd on purpose.** A realistic bar only rules out a target
    *inside* it, which turns this into "no target within ten points of the open" while the name
    claims "no target". A 1:1 target inventable from these levels sits at 1.10500 — outside a
    plausible hourly range and comfortably inside this one. The low stays well clear of the
    stop, so the only thing this bar can possibly fill is a target that should not exist.
    """
    broker = _broker()
    _open_a_long(broker, stop=None)
    assert broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE) is True

    assert (
        broker.on_bar(bar(2, open_="1.10000", close="1.10000", high="1.30000", low="1.09900")) == []
    )


def test_a_naive_decision_instant_is_refused_at_the_call() -> None:
    """Every other instant is validated where it is built; this one never becomes an
    `OrderRequest`, so nothing else would catch it. Accepted here it is stored, and surfaces
    bars later from inside the exit path — a traceback pointing at the fill, not the caller."""
    broker = _broker()
    _open_a_long(broker)

    with pytest.raises(ValueError, match="modify_stop decided_at"):
        broker.modify_stop("EURUSD", Decimal("1.09500"), dt.datetime(2024, 1, 1, 1))  # noqa: DTZ001


# --------------------------------------------------------------------------- #
# The moved stop, as the rest of the engine sees it (ADR-0018)
# --------------------------------------------------------------------------- #


def test_the_position_reports_the_stop_it_would_exit_at_today() -> None:
    """`Position.stop_loss` follows the modification; `initial_stop_loss` never does.

    Two facts that were one fact until a stop could move. The live level is what the strategy
    must read to know whether its next level tightens; the entry's level is what the lot was
    sized against, and therefore the only honest denominator for an R multiple.
    """
    broker = _broker()
    _open_a_long(broker)  # entry 1.10000, stop 1.09000

    broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE)

    [position] = broker.positions("EURUSD")
    assert position.stop_loss == Decimal("1.09500")
    assert position.initial_stop_loss == Decimal("1.09000")


def test_a_conduction_that_reads_its_own_stop_back_is_never_refused() -> None:
    """The author's conduction, end to end, with the guard an author actually writes.

    Breakeven at +2R, then "the stop goes to the average's extreme" — and the extreme named on
    a later bar can sit *below* the breakeven stop, because the average trails price rather
    than tracking it. So the strategy guards itself: only move if the new level tightens.

    That guard is only as good as what it reads. While `Position.stop_loss` reported the
    *entry's* stop, the comparison was made against a level that no longer existed: 1.09950
    beats the stale 1.09000, the signal goes out, and the engine — which knows the stop is
    really at 1.10000 — raises. A trade that is winning, on a bar that never threatens the
    stop, kills the whole backtest, and nothing the strategy could read would have saved it.
    """

    class Conduction:
        """Long once, then ratchet: breakeven at +2R, then the average's extreme."""

        def __init__(self) -> None:
            self.entered = False
            self.at_breakeven = False
            self.refused_to_loosen = False

        def on_bar(self, context: Context) -> list[Signal]:
            position = context.position
            if position is None:
                if self.entered:
                    return []
                self.entered = True
                return [
                    Signal(
                        kind=SignalKind.ENTRY,
                        side=Side.LONG,
                        reference_price=context.candle.close,
                        stop_loss=Decimal("1.09000"),  # 1R = 0.01000
                    )
                ]

            if not self.at_breakeven:
                if context.candle.high < Decimal("1.12000"):  # +2R not touched yet
                    return []
                self.at_breakeven = True
                return [
                    Signal(
                        kind=SignalKind.MODIFY_STOP,
                        side=Side.LONG,
                        reference_price=context.candle.close,
                        stop_loss=Decimal("1.10000"),
                        reason="breakeven at 2R",
                    )
                ]

            wanted = Decimal("1.09950")  # where the average sits on this bar
            current = position.stop_loss
            if current is not None and wanted <= current:
                self.refused_to_loosen = True
                return []
            return [
                Signal(
                    kind=SignalKind.MODIFY_STOP,
                    side=Side.LONG,
                    reference_price=context.candle.close,
                    stop_loss=wanted,
                    reason="trail to the average",
                )
            ]

    strategy = Conduction()
    result = run(
        candles=[
            bar(0, open_="1.10000", close="1.10000"),
            bar(1, open_="1.10000", close="1.10000"),
            # +2R touched: the stop goes to the entry price.
            bar(2, open_="1.10000", close="1.12000", high="1.12100", low="1.10000"),
            # Winning, and nowhere near the stop — but the average names 1.09950.
            bar(3, open_="1.12000", close="1.12100", high="1.12200", low="1.11900"),
            # Give it back: the breakeven stop is what closes the trade.
            bar(4, open_="1.11000", close="1.09000", high="1.11000", low="1.09000"),
        ],
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,
        broker=_broker(),
        risk=FixedRisk(),
    )

    # The guard fired — meaning it compared against 1.10000, the level really in force.
    assert strategy.refused_to_loosen is True

    [trade] = result.trades
    assert trade.exit_price == Decimal("1.10000")
    assert trade.reason == "sl"
    # Reported against the stop the lot was *sized* against, not the one it died at. Measured
    # against the moved stop the distance is zero, and this would be `None` instead of a
    # scratch — every trailed winner silently losing its R.
    assert trade.stop_loss == Decimal("1.09000")
    assert trade.r_multiple == Decimal(0)


def test_a_stop_armed_on_an_unstopped_position_leaves_the_trade_without_an_r() -> None:
    """Nothing sized this lot against a level, so the trade has no risk to be a multiple of.

    The live stop exists and is the one that closes the trade; `initial_stop_loss` stays
    `None`, and `r_multiple` follows it. Filling it in from the armed level would invent a
    denominator out of a decision taken after the money was already on the table.
    """
    broker = _broker()
    _open_a_long(broker, stop=None)
    broker.modify_stop("EURUSD", Decimal("1.09500"), BAR_ONE)

    [position] = broker.positions("EURUSD")
    assert (position.stop_loss, position.initial_stop_loss) == (Decimal("1.09500"), None)

    broker.on_bar(bar(2, open_="1.10000", close="1.09400", low="1.09400"))

    [trade] = broker.trades()
    assert trade.exit_price == Decimal("1.09500")
    assert trade.stop_loss is None
    assert trade.r_multiple is None


def test_a_stop_modification_with_no_position_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A strategy that trails onto an entry which has not filled yet gets `False` back, and
    the loop drops the instruction. Harmless — but silence here is indistinguishable from a
    trailing rule that ran and worked, which is the one thing the author is trying to see.

    The message says the broker refused and nothing more. `ImmediateFillBroker` returns `False`
    while *holding* a position — it keeps no protective level for a modification to reach — so
    a line blaming a missing position would be flatly wrong there.
    """
    strategy = ScriptedStrategy(
        script={
            0: [
                Signal(
                    kind=SignalKind.MODIFY_STOP,
                    side=Side.LONG,
                    reference_price=Decimal("1.10000"),
                    stop_loss=Decimal("1.09500"),
                    reason="trail with nothing to trail",
                )
            ]
        }
    )
    with caplog.at_level(logging.DEBUG, logger="tradeforge_engine.loop"):
        result = run(
            candles=[
                bar(0, open_="1.10000", close="1.10000"),
                bar(1, open_="1.10000", close="1.10000"),
            ],
            timeframe=HOUR,
            instrument=EURUSD,
            strategy=strategy,
            broker=_broker(),
            risk=FixedRisk(),
        )

    assert result.fills == ()
    assert "broker refused the stop modification" in caplog.text
    assert "trail with nothing to trail" in caplog.text


def test_a_stop_moved_past_the_market_exits_at_the_next_open_and_calls_it_sl() -> None:
    """The registered debt, pinned so it can only change on purpose (ADR-0018, `specs/backlog.md`).

    Nothing checks a new stop against where price actually is — only against the stop it
    replaces. So a long can be given a stop *above* the market: 1.13000 tightens against
    1.09000 by the only rule there is, and the next bar leaves at its open.

    No money is invented — 1.12000 is a price that really traded, and the clamp to the bar is
    what guarantees that. The damage is the label: a **winning** trade, +2R and +$2 000, files
    itself in the record as `reason='sl'`. A trailing rule with a sign error — naming the bar's
    high where it meant its low — produces exactly this and nothing raises anywhere.

    The mirror on `stop_loss` versus `limit_price`/`stop_price` has been open since ADR-0014;
    they close together, because closing only this one changes the limit order's contract too.
    """
    broker = _broker()
    _open_a_long(broker)  # entry 1.10000, stop 1.09000

    assert broker.modify_stop("EURUSD", Decimal("1.13000"), BAR_ONE) is True

    [fill] = broker.on_bar(bar(2, open_="1.12000", close="1.12100", high="1.12200", low="1.11900"))

    assert fill.price == Decimal("1.12000")  # the open, not the impossible 1.13000
    assert fill.order.reason == "sl"
    [trade] = broker.trades()
    assert (trade.net_pnl, trade.r_multiple) == (Decimal(2000), Decimal(2))


def test_a_tightened_stop_beats_a_strategy_exit_waiting_on_the_same_bar() -> None:
    """Both want to close the position on bar 2, and the stop is checked first.

    That is the engine's "worst case goes first" rule doing its job: a protective level is live
    from the bar's first tick, while a strategy's exit fills at the open. Reversing them would
    let a trade decided on the previous close escape a stop the market had already taken.

    Newly reachable, because until ADR-0018 the stop on that bar could only be the entry's. The
    strategy's exit is then discarded rather than left lurking — bar 3 fills nothing, and the
    run ends with one trade, not a short opened by an exit that outlived its position.
    """
    broker = _broker()
    _open_a_long(broker)  # entry 1.10000, stop 1.09000
    broker.modify_stop("EURUSD", Decimal("1.09800"), BAR_ONE)
    broker.submit(
        OrderRequest(
            symbol="EURUSD",
            side=Side.SHORT,
            intent=SignalKind.EXIT,
            volume=Decimal(1),
            decided_at=BAR_ONE,
            reason="strategy exit",
        )
    )

    [fill] = broker.on_bar(bar(2, open_="1.10000", close="1.09750", high="1.10000", low="1.09700"))

    assert fill.price == Decimal("1.09800")  # the moved stop, not the 1.10000 open
    assert fill.order.reason == "sl"
    assert broker.on_bar(bar(3, open_="1.09700", close="1.09700")) == []
    assert len(broker.trades()) == 1


def test_a_stop_trailed_past_the_target_leaves_the_target_in_charge() -> None:
    """Nothing stops a trail from climbing over the take-profit, and nothing needs to.

    Entry 1.10000, stop 1.09000, `rr=2` — the target sits at 1.12000. Trail the stop to 1.12500
    and a long now holds a stop *above* its own target, which reads like a contradiction. It is
    not reachable trouble: to touch a stop up there the market must first pass the target, so
    the target fires and the trade closes at 2.6R — better than 2R only because the bar opened
    through the level, which is ordinary gap behaviour for a target.

    Worth pinning rather than arguing about, because the alternative reading — refuse the
    modification, or drag the target along — would be a rule invented here with no method
    behind it. The stop keeps its own instant and the target keeps the entry's, which is the
    whole point of storing two.
    """
    broker = _broker(take_profit_rr=Decimal(2))
    _open_a_long(broker)

    assert broker.modify_stop("EURUSD", Decimal("1.12500"), BAR_ONE) is True

    [fill] = broker.on_bar(bar(2, open_="1.12600", close="1.12800", high="1.13000", low="1.12550"))

    assert fill.price == Decimal("1.12600")
    assert fill.order.reason == "tp"
    assert fill.order.decided_at == DECIDED  # the entry's — the target never moved
    [trade] = broker.trades()
    assert trade.r_multiple == Decimal("2.6")
