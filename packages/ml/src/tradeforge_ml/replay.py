"""Every entry a setup proposes, each traded on its own (ADR-0031): the event base without the
entries a running trade hid.

A backtest holds one position at a time, and the setup itself refuses to arm while it holds one
(`swing.py`: `if context.position is not None ...`). With no target the position is closed only by
its stop, and a trend can keep it open for years: on the MM9 base 83,433 M15 entries in 2015 and
10,605 in 2024, the difference being runs stuck in old trades. A filter has to judge every entry
the setup proposes, so this replays the setup **always flat** and trades each proposal alone:

* the setup — the engine's own, compiled from the run's document, unchanged — is shown every bar
  with no position, so it keeps proposing; the fill of its armed order is handed back to it on the
  bar it happens, so it treats that turn as spent exactly as it does in a run;
* each proposal is sent to a broker of its own — the engine's `BacktestBroker`, with the run's
  instrument, costs and swap — which decides the fill, the gap, the stop and the money by the same
  rules a run follows: nothing here re-implements them;
* each trade has two ways out: its **initial stop**, never moved (the setup's trail depends on the
  path, and the label should not), and the **time barrier**: `horizon` bars after the entry bar,
  closed at the next open;
* each proposal is also traded under **other stops** (`STOPS`, 14/10): the same order with its stop
  a multiple of the setup's distance away, each on a broker of its own (`Cell`). A target needs no
  broker of its own: it is read off each stop's excursion afterwards (`excursion.with_target`).

⚠️ **Not what the setup would have made live.** Live, a running trade would block the next entry
too. This is each proposal's own outcome — what a filter needs to judge — and the base says so in
its name (`independent-h<horizon>`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, localcontext
from typing import Any

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import (
    CombinedCostModel,
    CommissionCostModel,
    NoCostModel,
    ProportionalSpreadCostModel,
    SpreadCostModel,
)
from tradeforge_engine.domain import (
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Context,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    Position,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import CostModel, Strategy
from tradeforge_engine.swap import SwapRates

HORIZON = 150
"""Bars a trade may stay open after the bar it entered on (his choice, 06/10)."""

STOPS: tuple[Decimal, ...] = tuple(Decimal(k) for k in ("0.5", "0.75", "1", "1.5", "2", "3"))
"""The stops every proposal is also traded with, as multiples of the distance the setup put
between the order's level and its stop (his idea, 14/10): a setup that wins one trade in five can
still end positive with the right stop and target, and which ones is a question for each entry.
`1` is the setup's own stop — the proposal itself. Up to 3: on 9.4 BR the mean R still rose from
1.5 to 2 (-0.149 to -0.129, training years)."""

LOT = Decimal(1)
"""Every proposal is traded with one lot: R is money over the money at risk, so the size cancels;
the spread and the swap are per lot and scale with it."""

ACCOUNT = AccountState(balance=Decimal(10_000), equity=Decimal(10_000))
"""What the setup is shown of an account. The MM9 setups never read it; a flat replay has none."""


def instrument_from(kept: Mapping[str, Any]) -> InstrumentSpec:
    """The instrument a run executed with (`Backtest.instrument_spec`), as the runner reads it."""
    return InstrumentSpec(
        symbol=kept["symbol"],
        name=kept["name"],
        asset_class=AssetClass(kept["asset_class"]),
        currency_quote=kept["currency_quote"],
        currency_base=kept["currency_base"],
        tick_size=Decimal(kept["tick_size"]),
        tick_value=Decimal(kept["tick_value"]),
        contract_size=Decimal(kept["contract_size"]),
        digits=int(kept["digits"]),
        exchange=kept["exchange"],
        server_offset=dt.timedelta(hours=float(kept.get("server_offset_hours", 0))),
    )


def _decimal(value: Any) -> Decimal:  # noqa: ANN401 — a JSON number or string
    return Decimal(str(value))


def costs_from(spec: Mapping[str, Any]) -> CostModel:
    """A run's cost document (`Backtest.cost_model`) as the engine's model — the runner's
    `build_cost_model`, which a shared package may not import (`tests/test_architecture.py`)."""
    kind = spec.get("type")
    if kind not in {"none", "spread", "commission", "spread_commission"}:
        raise ValueError(f"unknown cost model type {kind!r}")
    if kind == "none":
        return NoCostModel()
    spread: CostModel | None = None
    if kind in {"spread", "spread_commission"}:
        reference = spec.get("spread_reference_price")
        spread = (
            SpreadCostModel(spread_points=_decimal(spec["spread_points"]))
            if reference is None
            else ProportionalSpreadCostModel(
                spread_points=_decimal(spec["spread_points"]), reference_price=_decimal(reference)
            )
        )
    if kind == "spread" and spread is not None:
        return spread
    commission = CommissionCostModel(commission_per_unit=_decimal(spec["commission_per_unit"]))
    if kind == "commission":
        return commission
    assert spread is not None  # noqa: S101 — the kinds left are the ones with a spread
    return CombinedCostModel(spread, commission)


PROPORTIONAL = frozenset({AssetClass.STOCK, AssetClass.CRYPTO})
"""The markets whose spread is charged in proportion to the price: their price moved by multiples
over the years a base covers. The API does it for a crypto (02/10); a share is the same case, and
in a split-adjusted history a worse one — NVDA's 2 ticks of $223 are 2.5 % of its 2016 price of
$0.80 in today's shares."""


def measured_costs(
    run_costs: Mapping[str, Any],
    *,
    spread_ticks: Decimal,
    asset_class: AssetClass,
    last_close: Decimal,
) -> dict[str, Any]:
    """What a label charges (14/10): the market's measured spread (`instruments.
    default_spread_points`), whatever the sweep was charged; its commission and swap are the run's.

    ADR-0031, rule 4, wants the label net of costs. The runs did not all pay them: five of the
    seventeen bases of 13/10 ran with none, and one had a spread typed per market at hundreds of
    times its own (ENGI11, 642 ticks against 1). A label for every base charges the same thing,
    and the cheapest thing a trade really pays is the spread.

    A share's and a crypto's are quoted at the chart's last close (`PROPORTIONAL`): the measured
    figure is today's, and the price it was measured at is about the newest bar's."""
    model: dict[str, Any] = {
        "type": "spread_commission",
        "spread_points": str(spread_ticks),
        "commission_per_unit": str(run_costs.get("commission_per_unit", "0")),
    }
    if asset_class in PROPORTIONAL:
        model["spread_reference_price"] = str(last_close)
    if run_costs.get("swap") is not None:
        model["swap"] = dict(run_costs["swap"])
    return model


def swap_from(spec: Mapping[str, Any]) -> SwapRates | None:
    swap = spec.get("swap")
    if swap is None:
        return None
    return SwapRates(
        long_per_lot=_decimal(swap.get("long_per_lot", 0)),
        short_per_lot=_decimal(swap.get("short_per_lot", 0)),
    )


@dataclass(frozen=True)
class Cell:
    """A proposal under one stop of the grid: the trade it made, or why it made none."""

    multiple: Decimal
    trade: ClosedTrade | None
    exit_reason: str
    """`sl` or `time`, as a proposal's. Without a trade:

    * `refused` — the broker would not fill the entry with this stop (a stop entry that slipped
      more than half of this stop's risk, a limit that gapped through it), or the order had no
      stop to scale. No trade is what a trader with this stop would have had;
    * `open` — the bars ran out before it closed. Not known, which is not the same as nothing."""


@dataclass(frozen=True)
class Proposal:
    """One entry the setup proposed and the broker filled, with how it ended."""

    trade: ClosedTrade
    exit_reason: str
    """`sl` when its stop closed it, `time` when the barrier did."""
    grid: tuple[Cell, ...] = ()
    """The entry under each stop of the replay's grid, in its order; `1` is `trade` itself.
    Empty for an entry before `entries_from`, which is left out anyway."""


def scaled_stop(order: OrderRequest, decided_close: Money, multiple: Decimal) -> Money:
    """`order`'s stop `multiple` times as far from where the order was planned to fill: its level
    — a limit's or a stop's — or, at market, the close it was decided on (`Signal.
    reference_price`, what sizing measures risk against). A trader places the stop with the
    order, before the fill is known; a fill that gaps away from the level changes the risk of
    every stop of the grid alike, as it does the setup's."""
    if order.stop_loss is None:
        raise ValueError("an order without a stop has no distance to scale")
    level = order.limit_price if order.limit_price is not None else order.stop_price
    planned = decided_close if level is None else level
    return planned - multiple * (planned - order.stop_loss)


def _time_exit(position: Position, candle: Candle) -> OrderRequest:
    """The time barrier's exit, decided at `candle`'s close and filled at the next open."""
    return OrderRequest(
        symbol=position.symbol,
        side=position.side,
        intent=SignalKind.EXIT,
        volume=position.volume,
        decided_at=candle.time,
        reason="time barrier",
    )


@dataclass
class _Ticket:
    """One proposal in flight: its own broker, from the order to the close."""

    broker: BacktestBroker
    order: OrderRequest
    stops: tuple[tuple[Decimal, Money | None], ...] = ()
    """The grid's other stops for this order, by multiple; `None` when it has no stop to scale."""
    entered_at: int | None = None
    """The index of the bar the entry filled on, once it has."""
    timed_out: bool = False
    grid: dict[Decimal, Cell] | None = None
    """The other stops' cells, followed when the entry filled; `None` before `entries_from`."""

    @property
    def client_id(self) -> str | None:
        return self.order.client_id


def _order(signal: Signal, candle: Candle, instrument: InstrumentSpec) -> OrderRequest:
    """The entry a run's loop would send (`loop._to_order`), at one lot and with no target."""
    return OrderRequest(
        symbol=instrument.symbol,
        side=signal.side,
        intent=SignalKind.ENTRY,
        volume=LOT,
        decided_at=candle.time,
        stop_loss=signal.stop_loss,
        reason=signal.reason,
        context=signal.context,
        limit_price=signal.limit_price,
        stop_price=signal.stop_price,
        client_id=signal.client_id,
    )


@dataclass
class Replay:
    """Drive `strategy` flat over `candles`, trading each proposal on its own broker — and, from
    the bar it fills on, on one more broker for each other stop of `stops`."""

    strategy: Strategy
    instrument: InstrumentSpec
    cost_model: CostModel
    swap: SwapRates | None = None
    horizon: int = HORIZON
    stops: Sequence[Decimal] = STOPS
    entries_from: dt.datetime | None = None
    """Entries before it are left out (`replay`), so their other stops are not followed."""
    cells: dict[tuple[object, ...], Cell] = field(default_factory=dict)
    """The other stops already followed, by what decides them — shared by the replays of one
    chart over the same bars at the same costs (`replay_export`). A sweep proposes most entries
    many times over: on 9.1 BR, 1.79 million proposals were 227 thousand entries."""
    done: list[Proposal] = field(default_factory=list)
    _tickets: list[_Ticket] = field(default_factory=list)
    _candles: Sequence[Candle] = ()

    def __post_init__(self) -> None:
        if Decimal(1) not in self.stops:
            raise ValueError("the grid must hold the setup's own stop (1): it is the proposal")
        if any(multiple <= 0 for multiple in self.stops):
            raise ValueError(f"a stop is a positive multiple of the setup's, got {self.stops}")

    def _broker(self) -> BacktestBroker:
        return BacktestBroker(
            instrument=self.instrument, cost_model=self.cost_model, swap=self.swap
        )

    def _step(self, index: int, candle: Candle) -> None:
        handed: list[Fill] = []
        for ticket in self._tickets:
            fills = ticket.broker.on_bar(candle)
            if ticket.entered_at is None and fills:
                ticket.entered_at = index
                handed.extend(fill for fill in fills if fill.order.intent is SignalKind.ENTRY)
                if self.entries_from is None or candle.time >= self.entries_from:
                    ticket.grid = self._grid(ticket, index)
        for ticket in list(self._tickets):
            if ticket.entered_at is None:
                continue
            if not ticket.broker.positions(self.instrument.symbol):
                (trade,) = ticket.broker.trades()
                reason = "time" if ticket.timed_out else "sl"
                grid = (
                    ()
                    if ticket.grid is None
                    else tuple(
                        Cell(multiple, trade, reason) if multiple == 1 else ticket.grid[multiple]
                        for multiple in self.stops
                    )
                )
                self.done.append(Proposal(trade, reason, grid))
                self._tickets.remove(ticket)
            elif index - ticket.entered_at >= self.horizon and not ticket.timed_out:
                (position,) = ticket.broker.positions(self.instrument.symbol)
                ticket.broker.submit(_time_exit(position, candle))
                ticket.timed_out = True

        context = Context(
            candle=candle, instrument=self.instrument, account=ACCOUNT, fills=tuple(handed)
        )
        for signal in self.strategy.on_bar(context):
            self._act(signal, candle)

    def _act(self, signal: Signal, candle: Candle) -> None:
        waiting = [ticket for ticket in self._tickets if ticket.entered_at is None]
        if signal.kind is SignalKind.CANCEL:
            for ticket in waiting:
                if signal.client_id is not None and ticket.client_id == signal.client_id:
                    ticket.broker.cancel(signal.client_id)
                    self._tickets.remove(ticket)
        elif signal.kind is SignalKind.ENTRY:
            broker = self._broker()
            order = _order(signal, candle, self.instrument)
            if broker.submit(order).accepted:
                stops = tuple(
                    (
                        multiple,
                        None
                        if order.stop_loss is None
                        else scaled_stop(order, signal.reference_price, multiple),
                    )
                    for multiple in self.stops
                    if multiple != 1
                )
                self._tickets.append(_Ticket(broker, order, stops))
        # MODIFY_STOP and EXIT act on a position, and a flat replay has none: the initial stop
        # and the time barrier are the only ways out.

    def _grid(self, ticket: _Ticket, filled_at: int) -> dict[Decimal, Cell]:
        """The entry under each other stop, followed from the bar it filled on — once per entry
        (`cells`): what decides a cell is the bars, the fill, the levels and the stop, never which
        run proposed it."""
        order, end = ticket.order, self._candles[-1].time
        grid: dict[Decimal, Cell] = {}
        for multiple, stop in ticket.stops:
            if stop is None:
                grid[multiple] = Cell(multiple, None, "refused")
                continue
            key = (
                end,
                self._candles[filled_at].time,
                order.side,
                order.limit_price,
                order.stop_price,
                stop,
                multiple,
            )
            cell = self.cells.get(key)
            if cell is None:
                cell = self.cells[key] = self._follow(order, filled_at, stop, multiple)
            grid[multiple] = cell
        return grid

    def _follow(self, order: OrderRequest, filled_at: int, stop: Money, multiple: Decimal) -> Cell:
        """`order` with `stop`, on a broker of its own, from the bar the setup's own filled on.

        ⚠️ **Submitted just before that bar, not when the setup decided it.** Only the stop
        differs, and the broker never reads a stop to decide a fill — only to refuse one — so the
        same order fills on the same bar at the same price, however long it rested before; and
        the bars it rested through are most of a zone's order's life. A cell that did not fill
        there was refused (`_survives_the_slip`, `_survives_the_gap`): a later fill would be
        another entry."""
        broker = self._broker()
        if not broker.submit(replace(order, stop_loss=stop)).accepted:
            return Cell(multiple, None, "refused")
        timed_out = False
        for index in range(filled_at, len(self._candles)):
            candle = self._candles[index]
            broker.on_bar(candle)
            if broker.trades():
                (trade,) = broker.trades()
                return Cell(multiple, trade, "time" if timed_out else "sl")
            positions = broker.positions(self.instrument.symbol)
            if not positions:
                return Cell(multiple, None, "refused")
            if index - filled_at >= self.horizon and not timed_out:
                broker.submit(_time_exit(positions[0], candle))
                timed_out = True
        return Cell(multiple, None, "open")

    def run(self, candles: Sequence[Candle]) -> list[Proposal]:
        self._candles = candles
        with localcontext(ENGINE_CONTEXT):
            for index, candle in enumerate(candles):
                self._step(index, candle)
        return self.done


def replay(  # noqa: PLR0913 — keyword-only; one per input a run is made of
    *,
    strategy: Strategy,
    candles: Sequence[Candle],
    instrument: InstrumentSpec,
    cost_model: CostModel,
    swap: SwapRates | None = None,
    horizon: int = HORIZON,
    entries_from: dt.datetime | None = None,
    stops: Sequence[Decimal] = STOPS,
    cells: dict[tuple[object, ...], Cell] | None = None,
) -> list[Proposal]:
    """The setup's proposals over `candles` in entry order, each closed by its stop or the time
    barrier, and each under every stop of `stops` too. Trades still open when the bars run out
    are left out: their outcome is not known. With `entries_from`, bars before it only warm the
    setup up — entries before it are left out. `cells` carries the stops already followed from
    one replay of these bars to the next."""
    done = Replay(
        strategy,
        instrument,
        cost_model,
        swap,
        horizon,
        stops,
        entries_from,
        {} if cells is None else cells,
    ).run(candles)
    # In entry order: the replay finishes trades in the order they close, and a long one entered
    # first can close after several entered behind it.
    return sorted(
        (
            proposal
            for proposal in done
            if entries_from is None or proposal.trade.entry_time >= entries_from
        ),
        key=lambda proposal: proposal.trade.entry_time,
    )


__all__ = [
    "HORIZON",
    "LOT",
    "PROPORTIONAL",
    "STOPS",
    "Cell",
    "Proposal",
    "Replay",
    "costs_from",
    "instrument_from",
    "measured_costs",
    "replay",
    "scaled_stop",
    "swap_from",
]
