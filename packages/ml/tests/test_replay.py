"""Every proposal traded on its own: flat to the setup, the engine's broker for the money."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.costs import (
    CombinedCostModel,
    NoCostModel,
    ProportionalSpreadCostModel,
    SpreadCostModel,
)
from tradeforge_engine.domain import Candle, Side, Signal, SignalKind
from tradeforge_engine.testing import EURUSD, ScriptedStrategy, entry, falling, rising
from tradeforge_ml.replay import Proposal, costs_from, instrument_from, replay, swap_from


def _run(
    strategy: ScriptedStrategy, bars: int = 30, horizon: int = 5, *, down: bool = False
) -> tuple[list[Candle], list[Proposal]]:
    candles = falling(bars) if down else rising(bars)
    done = replay(
        strategy=strategy,
        candles=candles,
        instrument=EURUSD,
        cost_model=NoCostModel(),
        horizon=horizon,
    )
    return candles, done


def test_a_proposal_a_run_would_block_is_traded_too() -> None:
    """The point of the replay: a second entry while the first is still open."""
    strategy = ScriptedStrategy(
        script={0: [entry(stop="1.00000")], 2: [entry(stop="1.00000", price="1.10300")]}
    )

    candles, done = _run(strategy)

    assert len(done) == 2
    assert [p.trade.entry_time for p in done] == [candles[1].time, candles[3].time]
    assert set(strategy.positions_seen) == {None}  # never shown a position


def test_the_time_barrier_closes_at_the_open_after_the_horizon() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.00000")]})

    candles, (only,) = _run(strategy, horizon=5)

    # Filled at bar 1's open; bar 6 is the fifth bar after it, decided at its close, so out at 7.
    assert only.exit_reason == "time"
    assert only.trade.entry_time == candles[1].time
    assert only.trade.exit_time == candles[7].time
    assert only.trade.exit_price == candles[7].open


def test_the_initial_stop_closes_it_and_the_r_is_the_brokers() -> None:
    strategy = ScriptedStrategy(script={0: [entry(price="1.19900", stop="1.19700")]})

    _, (only,) = _run(strategy, down=True)

    assert only.exit_reason == "sl"
    assert only.trade.r_multiple is not None
    assert only.trade.r_multiple <= Decimal(-1)


def test_the_fill_is_handed_to_the_setup_on_the_bar_it_happens() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.00000", client_id="a")]})

    _run(strategy)

    assert strategy.fills_seen[0] == ()
    (fill,) = strategy.fills_seen[1]
    assert fill.order.client_id == "a"
    assert all(seen == () for seen in strategy.fills_seen[2:])


def test_a_withdrawn_order_is_no_proposal() -> None:
    resting = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10100"),
        stop_price=Decimal("9.00000"),  # never reached
        stop_loss=Decimal("1.00000"),
        client_id="far",
    )
    cancel = Signal(
        kind=SignalKind.CANCEL, side=Side.LONG, reference_price=Decimal("1.1"), client_id="far"
    )
    strategy = ScriptedStrategy(script={0: [resting], 3: [cancel]})

    _, done = _run(strategy)

    assert done == []


def test_a_trade_still_open_when_the_bars_run_out_is_left_out() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.00000")], 26: [entry(stop="1.00000")]})

    _, done = _run(strategy, bars=30, horizon=5)

    assert len(done) == 1


def test_bars_before_entries_from_only_warm_the_setup() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.00000")], 10: [entry(stop="1.00000")]})
    candles = rising(30)

    done = replay(
        strategy=strategy,
        candles=candles,
        instrument=EURUSD,
        cost_model=NoCostModel(),
        horizon=5,
        entries_from=candles[5].time,
    )

    assert [p.trade.entry_time for p in done] == [candles[11].time]


class TestTheRunsDocuments:
    def test_costs_as_the_runner_builds_them(self) -> None:
        assert isinstance(costs_from({"type": "none"}), NoCostModel)
        assert isinstance(costs_from({"type": "spread", "spread_points": "15"}), SpreadCostModel)
        proportional = costs_from(
            {"type": "spread", "spread_points": "3000", "spread_reference_price": "60000"}
        )
        assert isinstance(proportional, ProportionalSpreadCostModel)
        both = {"type": "spread_commission", "spread_points": "2", "commission_per_unit": "7"}
        assert isinstance(costs_from(both), CombinedCostModel)
        with pytest.raises(ValueError, match="unknown"):
            costs_from({"type": "bribe"})

    def test_the_swap_and_the_instrument_a_run_kept(self) -> None:
        swap = swap_from({"type": "spread", "swap": {"long_per_lot": "-6.86"}})
        assert swap is not None
        assert (swap.long_per_lot, swap.short_per_lot) == (Decimal("-6.86"), Decimal(0))
        assert swap_from({"type": "none"}) is None

        kept = {
            "symbol": "EURUSD",
            "name": "Euro vs US Dollar",
            "asset_class": "forex",
            "currency_quote": "USD",
            "currency_base": "EUR",
            "tick_size": "0.00001",
            "tick_value": "1",
            "contract_size": "100000",
            "digits": 5,
            "exchange": None,
            "server_offset_hours": 3,
        }
        spec = instrument_from(kept)
        assert spec.tick_size == Decimal("0.00001")
        assert spec.server_offset == dt.timedelta(hours=3)


def test_a_stop_hit_on_the_entry_bar_hands_the_setup_its_entry_alone() -> None:
    """Falling bars: filled at 1.19900, the bar's low 1.19800 takes the stop at 1.19850 too."""
    strategy = ScriptedStrategy(script={0: [entry(price="1.19900", stop="1.19850", client_id="a")]})

    candles, (only,) = _run(strategy, down=True)

    (fill,) = strategy.fills_seen[1]
    assert fill.order.intent is SignalKind.ENTRY
    assert only.exit_reason == "sl"
    assert only.trade.exit_time == candles[1].time
