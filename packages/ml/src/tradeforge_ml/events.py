"""The event base of meta-labeling (ADR-0031): one row per distinct entry a setup took.

A sweep runs every point of its grid, and most points differ only in how they close a trade — a
target, a breakeven. Those points open the very same entries (PR-370/371), and counting each once
per exit would count one decision many times. The label of an entry is read from the run that did
**not** manage it: no target, no breakeven. There the trade runs to its stop or to the setup's own
rule, so its R and how far it went (MFE, MAE) tell the whole story, and the result under any target
can be scored from them afterwards (`excursion.target_ladder`).

Pure: no database, no files. `export` reads and writes; this decides.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

EXIT_PATHS = ("exit.take_profit.params.rr", "setup.params.breakeven_at_r")
"""The grid axes that manage a trade rather than choose it: left out of an entry's
configuration."""


def is_unmanaged(definition: Mapping[str, Any]) -> bool:
    """Whether a strategy document lets its trades run untouched: no target and no breakeven."""
    exit_block = definition.get("exit")
    target = exit_block.get("take_profit") if isinstance(exit_block, Mapping) else None
    setup = definition.get("setup")
    params = setup.get("params") if isinstance(setup, Mapping) else None
    breakeven = params.get("breakeven_at_r") if isinstance(params, Mapping) else None
    return target is None and breakeven is None


def entry_configuration(coordinates: Mapping[str, Any]) -> dict[str, Any]:
    """A point's coordinates without the axes that only manage the trade, and without the chart,
    which the event row carries on its own."""
    return {
        key: value
        for key, value in sorted(coordinates.items())
        if key not in EXIT_PATHS and key != "timeframe"
    }


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One trade of an unmanaged run, with what the event row needs of it and of its run."""

    run_id: str
    entry_id: str
    symbol: str
    timeframe: str
    configuration: Mapping[str, Any]
    side: str
    entry_time: Any
    entry_price: Any
    stop_loss: Any
    exit_time: Any
    exit_price: Any
    exit_reason: str | None
    r_multiple: Any
    mfe_r: Any
    mae_r: Any
    gross_pnl: Any
    costs: Any
    swap: Any
    net_pnl: Any
    volume: Any

    @property
    def key(self) -> tuple[Any, ...]:
        """What makes two trades one entry: the setup, the market, the chart, the instant, the
        side, and the levels it was taken at."""
        return (
            self.entry_id,
            self.symbol,
            self.timeframe,
            self.entry_time,
            self.side,
            self.entry_price,
            self.stop_loss,
        )

    @property
    def outcome(self) -> tuple[Any, ...]:
        return (self.exit_time, self.exit_price, self.r_multiple)


def distinct_entries(trades: Iterable[TradeRecord]) -> list[dict[str, Any]]:
    """One row per distinct entry, in the order first seen (launch order, if the trades come so).

    ⚠️ **The configurations that took it travel with it.** A long-average filter of 100 and one of
    200 can take the same trade; the event is one, and which configurations accepted it is a
    feature, not a second event.

    ⚠️ **The outcome is the first run's, and a disagreement is said.** Two unmanaged runs that took
    the same entry should close it the same way. When they do not — a setup whose later decisions
    depend on its configuration can close a trade earlier — `outcomes_agree` is false and the row
    keeps the first, so a model never trains on a label the base itself contradicts unknowingly.
    """
    rows: dict[tuple[Any, ...], dict[str, Any]] = {}
    outcomes: dict[tuple[Any, ...], tuple[Any, ...]] = {}
    for trade in trades:
        key = trade.key
        configuration = dict(trade.configuration)
        found = rows.get(key)
        if found is None:
            rows[key] = {
                "entry_id": trade.entry_id,
                "symbol": trade.symbol,
                "timeframe": trade.timeframe,
                "side": trade.side,
                "entry_time": trade.entry_time,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "exit_time": trade.exit_time,
                "exit_price": trade.exit_price,
                "exit_reason": trade.exit_reason,
                "r_multiple": trade.r_multiple,
                "mfe_r": trade.mfe_r,
                "mae_r": trade.mae_r,
                "gross_pnl": trade.gross_pnl,
                "costs": trade.costs,
                "swap": trade.swap,
                "net_pnl": trade.net_pnl,
                "volume": trade.volume,
                "first_run_id": trade.run_id,
                "configurations": [configuration],
                "outcomes_agree": True,
            }
            outcomes[key] = trade.outcome
            continue
        if configuration not in found["configurations"]:
            found["configurations"].append(configuration)
        if trade.outcome != outcomes[key]:
            found["outcomes_agree"] = False
    return list(rows.values())


def reconciles(
    trades: Sequence[TradeRecord],
    *,
    total_trades: int,
    net_r: Decimal | float | None,
    tolerance: float = 1e-6,
) -> bool:
    """Whether a run's exported trades are its recorded ones: as many, and their R summing to the
    run's net R — the check that nothing was lost between the database and the file."""
    if len(trades) != total_trades:
        return False
    if net_r is None:
        return all(trade.r_multiple is None for trade in trades)
    summed = sum(float(trade.r_multiple) for trade in trades if trade.r_multiple is not None)
    return abs(summed - float(net_r)) <= tolerance * max(1.0, abs(float(net_r)))


__all__ = [
    "EXIT_PATHS",
    "TradeRecord",
    "distinct_entries",
    "entry_configuration",
    "is_unmanaged",
    "reconciles",
]
