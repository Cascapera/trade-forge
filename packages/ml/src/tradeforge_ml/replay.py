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
  closed at the next open.

⚠️ **Not what the setup would have made live.** Live, a running trade would block the next entry
too. This is each proposal's own outcome — what a filter needs to judge — and the base says so in
its name (`independent-h<horizon>`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
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
    OrderRequest,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import CostModel, Strategy
from tradeforge_engine.swap import SwapRates

HORIZON = 150
"""Bars a trade may stay open after the bar it entered on (his choice, 06/10)."""

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


def swap_from(spec: Mapping[str, Any]) -> SwapRates | None:
    swap = spec.get("swap")
    if swap is None:
        return None
    return SwapRates(
        long_per_lot=_decimal(swap.get("long_per_lot", 0)),
        short_per_lot=_decimal(swap.get("short_per_lot", 0)),
    )


@dataclass(frozen=True)
class Proposal:
    """One entry the setup proposed and the broker filled, with how it ended."""

    trade: ClosedTrade
    exit_reason: str
    """`sl` when its stop closed it, `time` when the barrier did."""


@dataclass
class _Ticket:
    """One proposal in flight: its own broker, from the order to the close."""

    broker: BacktestBroker
    client_id: str | None
    entered_at: int | None = None
    """The index of the bar the entry filled on, once it has."""
    timed_out: bool = False


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
    """Drive `strategy` flat over `candles`, trading each proposal on its own broker."""

    strategy: Strategy
    instrument: InstrumentSpec
    cost_model: CostModel
    swap: SwapRates | None = None
    horizon: int = HORIZON
    done: list[Proposal] = field(default_factory=list)
    _tickets: list[_Ticket] = field(default_factory=list)

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
        for ticket in list(self._tickets):
            if ticket.entered_at is None:
                continue
            if not ticket.broker.positions(self.instrument.symbol):
                (trade,) = ticket.broker.trades()
                self.done.append(Proposal(trade, "time" if ticket.timed_out else "sl"))
                self._tickets.remove(ticket)
            elif index - ticket.entered_at >= self.horizon and not ticket.timed_out:
                (position,) = ticket.broker.positions(self.instrument.symbol)
                ticket.broker.submit(
                    OrderRequest(
                        symbol=self.instrument.symbol,
                        side=position.side,
                        intent=SignalKind.EXIT,
                        volume=position.volume,
                        decided_at=candle.time,
                        reason="time barrier",
                    )
                )
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
            if broker.submit(_order(signal, candle, self.instrument)).accepted:
                self._tickets.append(_Ticket(broker, signal.client_id))
        # MODIFY_STOP and EXIT act on a position, and a flat replay has none: the initial stop
        # and the time barrier are the only ways out.

    def run(self, candles: Iterable[Candle]) -> list[Proposal]:
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
) -> list[Proposal]:
    """The setup's proposals over `candles` in entry order, each closed by its stop or the time
    barrier. Trades still open when the bars run out are left out: their outcome is not known.
    With `entries_from`, bars before it only warm the setup up — entries before it are left out."""
    done = Replay(strategy, instrument, cost_model, swap, horizon).run(candles)
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
    "Proposal",
    "Replay",
    "costs_from",
    "instrument_from",
    "replay",
    "swap_from",
]
