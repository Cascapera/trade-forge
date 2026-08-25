"""Drive the engine for one backtest — the impure I/O (DB, queue, Redis) kept out.

Given a strategy document, an instrument, a capital and the candles, this assembles the same
object graph the golden test builds by hand — `InstrumentSpec`, a `CostModel`, a
`BacktestBroker`, a `PercentRiskManager`, `compile_strategy` — runs it, and returns the trades
and the §5 metrics. It reaches for no database and no queue, so the translation from a *stored*
run into an *engine* run is unit-tested against plain candles, without either.

Two numbers live in the DSL and must be pulled out here rather than by `compile_strategy`,
because the engine's boundary puts them elsewhere: the risk `percent` (the `PercentRiskManager`
is a separate argument to `run`) and the take-profit `rr` (the target is the broker's, resolved
at fill — the PR-104/105 boundary). DSL numbers arrive from JSONB as float/int and are parsed
via `str`, so `1.1` stays `1.1` and never inherits a float's binary dust.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, NamedTuple

from tradeforge_collector import step
from tradeforge_db.models import Instrument
from tradeforge_engine import (
    BacktestBroker,
    Candle,
    ClosedTrade,
    CommissionCostModel,
    InstrumentSpec,
    NoCostModel,
    PercentRiskManager,
    SpreadCostModel,
    compile_strategy,
    compute_metrics,
    run,
)
from tradeforge_engine import BacktestMetrics as EngineMetrics
from tradeforge_engine import __version__ as ENGINE_VERSION  # noqa: N812 — public constant
from tradeforge_engine.protocols import CostModel


def _decimal(value: object) -> Decimal:
    # DSL numbers arrive from JSONB as float/int; via `str` so `1.1` stays `1.1`, not `1.1000…04`.
    return Decimal(str(value))


def instrument_spec(instrument: Instrument) -> InstrumentSpec:
    """The engine's value object, projected from the database row. The `asset_class` enum is
    the very same `tradeforge_engine.domain.AssetClass` the ORM column stores — no translation."""
    return InstrumentSpec(
        symbol=instrument.symbol,
        name=instrument.name,
        asset_class=instrument.asset_class,
        currency_quote=instrument.currency_quote,
        currency_base=instrument.currency_base,
        tick_size=instrument.tick_size,
        tick_value=instrument.tick_value,
        contract_size=instrument.contract_size,
        digits=instrument.digits,
        exchange=instrument.exchange,
    )


def build_cost_model(spec: Mapping[str, Any]) -> CostModel:
    """Build the plugged-in cost model from its stored document (ADR-07).

       Public because a live session assembles the same object graph: the DSL-to-engine
       translation is the boundary, not the backtest.
    An unknown type
       raises rather than silently running costless — the same loud-failure doctrine as the
       exit-reason mapper and `build_indicator`."""
    kind = spec.get("type")
    if kind == "none":
        return NoCostModel()
    if kind == "spread":
        return SpreadCostModel(spread_points=_decimal(spec["spread_points"]))
    if kind == "commission":
        return CommissionCostModel(commission_per_unit=_decimal(spec["commission_per_unit"]))
    raise ValueError(f"unknown cost model type {kind!r}; expected 'none', 'spread' or 'commission'")


def take_profit_rr(definition: Mapping[str, Any]) -> Decimal | None:
    take_profit = (definition.get("exit") or {}).get("take_profit")
    if not take_profit:
        return None
    rr = (take_profit.get("params") or {}).get("rr")
    return _decimal(rr) if rr is not None else None


def risk_percent(definition: Mapping[str, Any]) -> Decimal:
    sizing = (definition.get("risk") or {}).get("sizing") or {}
    percent = (sizing.get("params") or {}).get("percent")
    if percent is None:
        raise ValueError("strategy declares no percent_risk sizing; positions cannot be sized")
    return _decimal(percent)


def _candles_to_run(
    candles: Sequence[Candle],
    symbol: str,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> list[Candle]:
    """Clip to the requested window — and refuse to run over nothing.

    Reading the whole (symbol, timeframe) and filtering here is fine for phase 1; partition
    pruning by year is a later optimisation, not a correctness need.

    The refusal is the point. A run with no candles used to finish as `done` with every metric
    at zero, which reads exactly like a run whose strategy found no setups. Those two need
    opposite responses — collect the data, versus nothing to do — and a screen full of zeroes
    cannot say which you are looking at.

    Note the line is drawn at *candles*, not at trades. A window full of candles that produces
    no trades is a real answer and still succeeds; conflating the two would turn "my filter is
    too strict" into an error message.
    """
    if not candles:
        raise LookupError(
            f"no candles have been collected for {symbol} {timeframe}; "
            f"run the collector backfill for it before backtesting"
        )

    windowed = [candle for candle in candles if date_from <= candle.time <= date_to]
    if not windowed:
        raise LookupError(
            f"{symbol} {timeframe} covers {candles[0].time:%Y-%m-%d} to "
            f"{candles[-1].time:%Y-%m-%d}, and the requested window "
            f"{date_from:%Y-%m-%d} to {date_to:%Y-%m-%d} contains none of it"
        )
    return windowed


class CandleWindow(NamedTuple):
    """What a run actually read, as opposed to what it asked for.

    Returned alongside the result so the run can be *recorded* honestly. A request is not
    evidence: asking for 2024-01-01 onwards when the dataset opens in August produces a
    perfectly ordinary-looking result covering five months, and without this nothing
    downstream can tell that from the two years the row claims.
    """

    # Not `count`: a NamedTuple inherits `tuple.count`, and a field of that name would shadow
    # the method — mypy rejects it outright rather than let the two quietly disagree.
    candles: int
    first: dt.datetime
    last: dt.datetime


def execute_backtest(  # noqa: PLR0913 — keyword-only; each names one axis of a backtest run
    *,
    definition: Mapping[str, Any],
    instrument: Instrument,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
    initial_capital: Decimal,
    cost_model: Mapping[str, Any],
    slippage_ticks: Decimal,
    candles: Sequence[Candle],
) -> tuple[list[ClosedTrade], EngineMetrics, CandleWindow]:
    """Run the strategy over the windowed candles and fold the result into the §5 metrics.

    Returns the window it actually read along with the result, so the caller can record what
    the run saw rather than what it was asked for.
    """
    windowed = _candles_to_run(candles, instrument.symbol, timeframe, date_from, date_to)

    spec = instrument_spec(instrument)
    broker = BacktestBroker(
        instrument=spec,
        initial_capital=initial_capital,
        cost_model=build_cost_model(cost_model),
        slippage_ticks=slippage_ticks,
        take_profit_rr=take_profit_rr(definition),
    )
    result = run(
        candles=windowed,
        timeframe=step(timeframe),
        instrument=spec,
        strategy=compile_strategy(definition),
        broker=broker,
        risk=PercentRiskManager(percent=risk_percent(definition)),
    )
    metrics = compute_metrics(
        trades=result.trades,
        equity_curve=result.equity_curve,
        initial_capital=initial_capital,
    )
    window = CandleWindow(len(windowed), windowed[0].time, windowed[-1].time)
    return list(result.trades), metrics, window


__all__ = [
    "ENGINE_VERSION",
    "CandleWindow",
    "build_cost_model",
    "execute_backtest",
    "instrument_spec",
    "risk_percent",
    "take_profit_rr",
]
