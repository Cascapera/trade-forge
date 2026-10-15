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
from tradeforge_engine.domain import (
    AssetClass,
    Candle,
    OrderRequest,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.testing import EURUSD, START, ScriptedStrategy, bar, entry, falling, rising
from tradeforge_ml.replay import (
    STOPS,
    Cell,
    Proposal,
    Replay,
    costs_from,
    instrument_from,
    measured_costs,
    replay,
    scaled_stop,
    swap_from,
)


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
    assert [cell.multiple for cell in done[0].grid] == list(STOPS)


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


class TestTheMeasuredCosts:
    """What a label charges since 14/10: the market's measured spread, whatever the sweep was."""

    def test_the_markets_spread_replaces_the_runs_and_its_commission_and_swap_stay(self) -> None:
        typed = {
            "type": "spread_commission",
            "spread_points": "642",  # ENGI11's in sweep d2faf8dc, against 1 measured
            "commission_per_unit": "7",
            "swap": {"long_per_lot": "-6.86", "short_per_lot": "0.86"},
        }

        model = measured_costs(
            typed, spread_ticks=Decimal(1), asset_class=AssetClass.FOREX, last_close=Decimal(30)
        )

        assert model == {
            "type": "spread_commission",
            "spread_points": "1",
            "commission_per_unit": "7",
            "swap": {"long_per_lot": "-6.86", "short_per_lot": "0.86"},
        }
        assert isinstance(costs_from(model), CombinedCostModel)
        assert swap_from(model) is not None

    def test_a_run_charged_nothing_pays_the_spread_all_the_same(self) -> None:
        model = measured_costs(
            {"type": "none"},
            spread_ticks=Decimal(100),
            asset_class=AssetClass.INDEX,
            last_close=Decimal(6700),
        )

        assert model == {
            "type": "spread_commission",
            "spread_points": "100",
            "commission_per_unit": "0",
        }

    @pytest.mark.parametrize("asset_class", [AssetClass.STOCK, AssetClass.CRYPTO])
    def test_a_share_and_a_crypto_pay_it_at_the_charts_last_price(
        self, asset_class: AssetClass
    ) -> None:
        model = measured_costs(
            {"type": "none"},
            spread_ticks=Decimal(2),
            asset_class=asset_class,
            last_close=Decimal("223.44"),
        )

        assert model["spread_reference_price"] == "223.44"


def test_a_stop_hit_on_the_entry_bar_hands_the_setup_its_entry_alone() -> None:
    """Falling bars: filled at 1.19900, the bar's low 1.19800 takes the stop at 1.19850 too."""
    strategy = ScriptedStrategy(script={0: [entry(price="1.19900", stop="1.19850", client_id="a")]})

    candles, (only,) = _run(strategy, down=True)

    (fill,) = strategy.fills_seen[1]
    assert fill.order.intent is SignalKind.ENTRY
    assert only.exit_reason == "sl"
    assert only.trade.exit_time == candles[1].time


# --------------------------------------------------------------------------------------------
# The grid of stops (14/10): every proposal under other stops, on brokers of their own
# --------------------------------------------------------------------------------------------


def _cells(proposal: Proposal) -> dict[Decimal, Cell]:
    return {cell.multiple: cell for cell in proposal.grid}


def test_each_stop_of_the_grid_is_the_setups_distance_times_its_multiple() -> None:
    """Falling bars a tenth of a cent each: a long at 1.19900 with its stop two bars below."""
    strategy = ScriptedStrategy(script={0: [entry(price="1.19900", stop="1.19700")]})

    candles, (only,) = _run(strategy, down=True, horizon=20)

    cells = _cells(only)
    assert list(cells) == list(STOPS)
    assert cells[Decimal(1)].trade is only.trade, "the setup's own stop is the proposal"
    stopped_at = {multiple: cell.trade.stop_loss for multiple, cell in cells.items() if cell.trade}
    assert stopped_at == {
        Decimal("0.5"): Decimal("1.19800"),
        Decimal("0.75"): Decimal("1.19750"),
        Decimal(1): Decimal("1.19700"),
        Decimal("1.5"): Decimal("1.19600"),
        Decimal(2): Decimal("1.19500"),
        Decimal(3): Decimal("1.19300"),
    }
    exits = [cell.trade.exit_time for cell in cells.values() if cell.trade]
    assert exits == [candles[n].time for n in (1, 2, 2, 3, 4, 6)], "a wider stop outlives"
    assert {cell.exit_reason for cell in cells.values()} == {"sl"}
    assert {cell.trade.r_multiple for cell in cells.values() if cell.trade} == {Decimal(-1)}


def test_a_limit_that_rested_is_followed_from_its_fill_as_if_it_had_rested_too() -> None:
    """The shortcut (`Replay._follow`): a buy limit at 1.19500 decided on bar 0 rests until the
    falling market reaches it on bar 4. Its cell for twice the stop is the trade that very order,
    stop 1.19100, makes resting from bar 0 on a broker of its own."""

    def limit(stop: str) -> Signal:
        return Signal(
            kind=SignalKind.ENTRY,
            side=Side.LONG,
            reference_price=Decimal("1.19900"),
            limit_price=Decimal("1.19500"),
            stop_loss=Decimal(stop),
            client_id="zone",
        )

    def proposed(stop: str, stops: tuple[Decimal, ...] = STOPS) -> Proposal:
        (only,) = replay(
            strategy=ScriptedStrategy(script={0: [limit(stop)]}),
            candles=falling(30),
            instrument=EURUSD,
            cost_model=SpreadCostModel(spread_points=Decimal(10)),
            horizon=20,
            stops=stops,
        )
        return only

    followed = _cells(proposed("1.19300"))[Decimal(2)]
    rested = proposed("1.19100", stops=(Decimal(1),))

    assert rested.trade.entry_time == falling(30)[4].time, "it rested four bars"
    assert (followed.trade, followed.exit_reason) == (rested.trade, rested.exit_reason)


def test_a_stop_too_tight_for_the_gap_is_refused_and_a_wider_one_is_not() -> None:
    """A buy stop at 1.10200 with its stop at 1.10000, and the next bar opens at 1.10300: half
    the setup's risk slipped, the most a stop entry may (`_survives_the_slip`). Half a stop
    slipped all of its own; one and a half, a third."""
    breakout = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10100"),
        stop_price=Decimal("1.10200"),
        stop_loss=Decimal("1.10000"),
        client_id="breakout",
    )
    candles = [
        bar(0, open_="1.10000", close="1.10100"),
        bar(1, open_="1.10300", close="1.10400", high="1.10450", low="1.10250"),
        *rising(9, start="1.10200")[2:],
    ]

    (only,) = replay(
        strategy=ScriptedStrategy(script={0: [breakout]}),
        candles=candles,
        instrument=EURUSD,
        cost_model=NoCostModel(),
        horizon=3,
    )

    reasons = {multiple: cell.exit_reason for multiple, cell in _cells(only).items()}
    assert reasons == {
        Decimal("0.5"): "refused",
        Decimal("0.75"): "refused",
        Decimal(1): "time",
        Decimal("1.5"): "time",
        Decimal(2): "time",
        Decimal(3): "time",
    }
    assert _cells(only)[Decimal("0.5")].trade is None


def test_a_wider_stop_still_open_when_the_bars_run_out_is_not_known() -> None:
    strategy = ScriptedStrategy(script={4: [entry(price="1.19500", stop="1.19300")]})

    _, (only,) = _run(strategy, bars=8, horizon=20, down=True)

    reasons = {multiple: cell.exit_reason for multiple, cell in _cells(only).items()}
    assert reasons[Decimal("1.5")] == "sl"
    assert reasons[Decimal(2)] == "open"
    assert _cells(only)[Decimal(2)].trade is None


def test_at_the_time_barrier_every_stop_leaves_together_and_its_r_is_its_own_risk() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.09900")]})

    _, (only,) = _run(strategy, horizon=5)

    cells = _cells(only)
    trades = [cell.trade for cell in cells.values()]
    assert {cell.exit_reason for cell in cells.values()} == {"time"}
    assert len({(trade.exit_time, trade.exit_price) for trade in trades if trade}) == 1
    own = only.trade.r_multiple
    assert own is not None
    for multiple, cell in cells.items():
        assert cell.trade is not None
        assert cell.trade.r_multiple is not None
        assert cell.trade.r_multiple * multiple == pytest.approx(own), "twice the risk, half the R"


def test_an_entry_proposed_again_on_the_same_bars_is_followed_once() -> None:
    """A sweep proposes most entries many times over, one run of the chart after another."""
    cells: dict[tuple[object, ...], Cell] = {}
    candles = falling(30)

    def proposed(script: dict[int, list[Signal]]) -> list[Proposal]:
        return replay(
            strategy=ScriptedStrategy(script=script),
            candles=candles,
            instrument=EURUSD,
            cost_model=NoCostModel(),
            horizon=20,
            cells=cells,
        )

    (first,) = proposed({0: [entry(price="1.19900", stop="1.19700")]})
    assert len(cells) == len(STOPS) - 1
    (again,) = proposed({0: [entry(price="1.19900", stop="1.19700")]})

    assert len(cells) == len(STOPS) - 1, "nothing followed twice"
    assert all(
        mine is theirs
        for mine, theirs in zip(first.grid, again.grid, strict=True)
        if mine.multiple != 1
    )
    proposed({3: [entry(price="1.19600", stop="1.19400")]})
    assert len(cells) == 2 * (len(STOPS) - 1)


def test_an_entry_before_entries_from_is_not_followed() -> None:
    strategy = ScriptedStrategy(script={0: [entry(stop="1.00000")]})
    candles = rising(30)
    replayed = Replay(
        strategy=strategy,
        instrument=EURUSD,
        cost_model=NoCostModel(),
        horizon=5,
        entries_from=candles[5].time,
    )

    (warming,) = replayed.run(candles)

    assert warming.grid == ()
    assert replayed.cells == {}


class TestTheScaledStop:
    """Where each stop of the grid goes: from the order's level, or the close it was decided
    on, never from a fill not known yet."""

    @staticmethod
    def order(
        side: Side,
        stop: str | None,
        *,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> OrderRequest:
        return OrderRequest(
            symbol="EURUSD",
            side=side,
            intent=SignalKind.ENTRY,
            volume=Decimal(1),
            decided_at=START,
            stop_loss=None if stop is None else Decimal(stop),
            limit_price=limit_price,
            stop_price=stop_price,
        )

    def test_a_limit_measures_from_its_level(self) -> None:
        sell = self.order(Side.SHORT, "1.20300", limit_price=Decimal("1.20000"))
        assert scaled_stop(sell, Decimal("1.19000"), Decimal(2)) == Decimal("1.20600")

    def test_a_stop_order_measures_from_its_trigger(self) -> None:
        buy = self.order(Side.LONG, "1.10000", stop_price=Decimal("1.10200"))
        assert scaled_stop(buy, Decimal("1.10100"), Decimal("1.5")) == Decimal("1.09900")

    def test_at_market_from_the_close_it_was_decided_on(self) -> None:
        buy = self.order(Side.LONG, "1.09500")
        assert scaled_stop(buy, Decimal("1.10000"), Decimal("0.5")) == Decimal("1.09750")

    def test_an_order_without_a_stop_has_no_distance(self) -> None:
        with pytest.raises(ValueError, match="no distance"):
            scaled_stop(self.order(Side.LONG, None), Decimal(1), Decimal(2))


def test_an_order_without_a_stop_is_refused_under_every_other_stop() -> None:
    strategy = ScriptedStrategy(script={0: [entry()]})

    _, (only,) = _run(strategy, horizon=3)

    reasons = {multiple: cell.exit_reason for multiple, cell in _cells(only).items()}
    assert reasons == {multiple: "refused" if multiple != 1 else "time" for multiple in STOPS}


@pytest.mark.parametrize("stops", [(Decimal("0.5"), Decimal(2)), (Decimal(0), Decimal(1))])
def test_a_grid_without_the_setups_stop_or_with_a_naught_is_refused(
    stops: tuple[Decimal, ...],
) -> None:
    with pytest.raises(ValueError, match="setup's"):
        Replay(ScriptedStrategy(), EURUSD, NoCostModel(), stops=stops)
