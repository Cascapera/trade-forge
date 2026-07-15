"""Cost models: what a trade costs, plugged in, never hard-coded (ADR-07).

The engine's P&L formula knows nothing about spreads or commissions. It multiplies a price
move by a tick value and stops there. Everything that makes a real fill cost money — the
spread you cross on a forex pair, the commission a broker charges on a share — arrives
through a `CostModel`, one object per way of charging.

Why this seam matters: forex pays a spread, a stock pays a commission, an index future pays
both, and a crypto venue charges a percentage. An `if asset_class == ...` inside the fill
site would spread that fork across the engine and grow a new branch with every venue. A cost
model is one class, and a new venue is a new class — not an edit to the core (AGENTS.md §5.6).

**Costs are a magnitude, not a signed number.** Every method here returns what *leaves* the
account, always ≥ 0. The `Fill` refuses a negative cost precisely so a sign bug here surfaces
at construction instead of as a strategy that mysteriously prints money.
"""

from decimal import Decimal

from tradeforge_engine.domain import ZERO, InstrumentSpec, Money, OrderRequest, Volume


class SpreadCostModel:
    """Forex: you cross half the spread on the way in and half on the way out.

    The spread is the gap between bid and ask. A buy fills at the ask, a sell at the bid, so
    the round trip pays the whole spread — modelled here as half on each leg. Expressed in
    *points* (ticks), which is how a broker quotes a spread, and converted to money the same
    way every P&L in the engine is: `(price_move / tick_size) · tick_value · volume`. Half a
    spread of `spread_points` ticks is `(spread_points / 2) · tick_value · volume`.

    Modelled as a **cost**, not as a worse fill price. Pushing the price instead would risk
    landing the fill outside the candle's own range — and the engine's lookahead guard
    (PR-103) rightly refuses a fill at a price the bar never traded at. The spread is real
    money leaving the account, so it belongs in `Fill.costs`, where the reconciliation
    property already accounts for it on both legs.
    """

    def __init__(self, *, spread_points: Decimal) -> None:
        if spread_points < ZERO:
            raise ValueError(f"spread must be a magnitude, got {spread_points}")
        self._spread_points = spread_points

    def _half_spread(self, instrument: InstrumentSpec, volume: Volume) -> Money:
        return (self._spread_points / 2) * instrument.tick_value * volume

    def entry_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return self._half_spread(instrument, order.volume)

    def exit_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return self._half_spread(instrument, order.volume)


class CommissionCostModel:
    """Stocks: a flat commission per unit traded, charged on each leg.

    `commission_per_unit` is money per lot (or per share — whatever the instrument's unit is),
    applied to the entry and again to the exit. No spread: a share's cost is the broker's fee,
    not a bid/ask crossing modelled here.
    """

    def __init__(self, *, commission_per_unit: Money) -> None:
        if commission_per_unit < ZERO:
            raise ValueError(f"commission must be a magnitude, got {commission_per_unit}")
        self._commission_per_unit = commission_per_unit

    def _commission(self, volume: Volume) -> Money:
        return self._commission_per_unit * volume

    def entry_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return self._commission(order.volume)

    def exit_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return self._commission(order.volume)


class NoCostModel:
    """No costs at all. For a golden test that isolates the fill logic from the cost logic —
    and for the degenerate baseline where you want to see the strategy's gross edge."""

    def entry_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return ZERO

    def exit_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:  # noqa: ARG002
        return ZERO


__all__ = ["CommissionCostModel", "NoCostModel", "SpreadCostModel"]
