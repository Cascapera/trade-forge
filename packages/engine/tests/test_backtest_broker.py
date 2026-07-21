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

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import SpreadCostModel
from tradeforge_engine.domain import OrderRequest, OrderResult, Side, Signal, SignalKind
from tradeforge_engine.loop import run
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
