"""The event base: which runs label an entry, and one row per distinct entry (ADR-0031)."""

import datetime as dt
from decimal import Decimal
from typing import Any

from tradeforge_ml.events import (
    TradeRecord,
    distinct_entries,
    entry_configuration,
    is_unmanaged,
    reconciles,
)

T0 = dt.datetime(2024, 3, 1, 10, tzinfo=dt.UTC)


def a_trade(run: str = "r1", **over: Any) -> TradeRecord:
    values: dict[str, Any] = {
        "run_id": run,
        "entry_id": "e1",
        "symbol": "EURUSD",
        "timeframe": "H1",
        "configuration": {"setup.params.long_average_period": 100},
        "side": "long",
        "entry_time": T0,
        "entry_price": Decimal("1.1"),
        "stop_loss": Decimal("1.09"),
        "exit_time": T0 + dt.timedelta(hours=5),
        "exit_price": Decimal("1.12"),
        "exit_reason": "sl",
        "r_multiple": Decimal("2"),
        "mfe_r": Decimal("3"),
        "mae_r": Decimal("0.5"),
        "gross_pnl": Decimal("200"),
        "costs": Decimal("5"),
        "swap": Decimal("0"),
        "net_pnl": Decimal("195"),
        "volume": Decimal("0.1"),
    }
    values.update(over)
    return TradeRecord(**values)


class TestUnmanaged:
    def test_no_target_and_no_breakeven_is_unmanaged(self) -> None:
        assert is_unmanaged({"setup": {"params": {"breakeven_at_r": None}}, "exit": {}})
        assert is_unmanaged({"setup": {"params": {}}})

    def test_a_target_or_a_breakeven_manages_the_trade(self) -> None:
        with_target = {"setup": {"params": {}}, "exit": {"take_profit": {"params": {"rr": 2}}}}
        with_breakeven = {"setup": {"params": {"breakeven_at_r": 1}}}

        assert not is_unmanaged(with_target)
        assert not is_unmanaged(with_breakeven)


def test_an_entrys_configuration_leaves_out_the_exit_and_the_chart() -> None:
    coordinates = {
        "timeframe": "H1",
        "exit.take_profit.params.rr": None,
        "setup.params.breakeven_at_r": None,
        "setup.params.side": "long",
        "setup.params.long_average_period": 100,
    }

    assert entry_configuration(coordinates) == {
        "setup.params.long_average_period": 100,
        "setup.params.side": "long",
    }


class TestDistinctEntries:
    def test_two_configurations_that_took_one_trade_are_one_event(self) -> None:
        """A long average of 100 and of 200 can take the very same trade: one decision."""
        rows = distinct_entries(
            [
                a_trade("r1"),
                a_trade("r2", configuration={"setup.params.long_average_period": 200}),
            ]
        )

        assert len(rows) == 1
        assert rows[0]["first_run_id"] == "r1"
        assert rows[0]["configurations"] == [
            {"setup.params.long_average_period": 100},
            {"setup.params.long_average_period": 200},
        ]
        assert rows[0]["outcomes_agree"] is True

    def test_another_instant_side_price_or_market_is_another_event(self) -> None:
        rows = distinct_entries(
            [
                a_trade(),
                a_trade(entry_time=T0 + dt.timedelta(hours=1)),
                a_trade(side="short"),
                a_trade(stop_loss=Decimal("1.08")),
                a_trade(symbol="GBPUSD"),
                a_trade(timeframe="H4"),
                a_trade(entry_id="e2"),
            ]
        )

        assert len(rows) == 7

    def test_a_disagreement_on_the_outcome_is_said_and_the_first_is_kept(self) -> None:
        rows = distinct_entries(
            [a_trade("r1"), a_trade("r2", r_multiple=Decimal("-1"), exit_price=Decimal("1.09"))]
        )

        assert rows[0]["outcomes_agree"] is False
        assert rows[0]["r_multiple"] == Decimal("2")

    def test_one_configuration_seen_twice_is_listed_once(self) -> None:
        rows = distinct_entries([a_trade("r1"), a_trade("r2")])

        assert rows[0]["configurations"] == [{"setup.params.long_average_period": 100}]


class TestReconciles:
    def test_as_many_trades_whose_r_sums_to_the_runs_net_r(self) -> None:
        trades = [a_trade(r_multiple=Decimal("2")), a_trade(r_multiple=Decimal("-1"))]

        assert reconciles(trades, total_trades=2, net_r=Decimal("1"))

    def test_a_missing_trade_or_a_different_sum_does_not(self) -> None:
        trades = [a_trade(r_multiple=Decimal("2"))]

        assert not reconciles(trades, total_trades=2, net_r=Decimal("2"))
        assert not reconciles(trades, total_trades=1, net_r=Decimal("2.5"))

    def test_a_run_with_no_r_reconciles_only_with_trades_with_no_r(self) -> None:
        assert reconciles([a_trade(r_multiple=None)], total_trades=1, net_r=None)
        assert not reconciles([a_trade()], total_trades=1, net_r=None)
