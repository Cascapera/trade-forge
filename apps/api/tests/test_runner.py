"""The engine-driving glue, unit-tested against plain candles — no database, no queue.

`execute_backtest` is the translation from a stored run into an engine run: it builds the
`InstrumentSpec`, the cost model, the broker and the risk manager, and folds the result into
the §5 metrics. Here it is exercised on a hand-built series, so a mistake in that wiring shows
up in milliseconds without Postgres or arq.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.runner import execute_backtest
from tradeforge_db.models import Instrument
from tradeforge_engine import BacktestMetrics as EngineMetrics
from tradeforge_engine.domain import AssetClass, Candle, ClosedTrade
from tradeforge_engine.testing import bar

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def an_instrument() -> Instrument:
    """A detached ORM row used only as a data holder — `execute_backtest` reads its fields, it
    never touches a session, so no database is involved."""
    return Instrument(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )


def dip_then_rally() -> list[Candle]:
    """Down a little, then a clean run up — so SMA(2) crosses above SMA(3) once and a long
    with a 2R target fires and reaches it."""
    levels = [
        "1.10500",
        "1.10400",
        "1.10300",
        "1.10200",
        "1.10300",
        "1.10500",
        "1.10800",
        "1.11200",
        "1.11700",
        "1.12300",
        "1.13000",
        "1.13800",
    ]
    return [
        bar(index, open_=levels[index], close=levels[index + 1]) for index in range(len(levels) - 1)
    ]


def ma_cross() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "MA cross",
        "timeframe": "H1",
        "indicators": [
            {"id": "fast", "type": "SMA", "params": {"period": 2}},
            {"id": "slow", "type": "SMA", "params": {"period": 3}},
        ],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
            "short": None,
        },
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def run_it(**overrides: object) -> tuple[list[ClosedTrade], EngineMetrics]:
    kwargs: dict[str, object] = {
        "definition": ma_cross(),
        "instrument": an_instrument(),
        "timeframe": "H1",
        "date_from": START,
        "date_to": START + 100 * HOUR,
        "initial_capital": Decimal("10000"),
        "cost_model": {"type": "none"},
        "slippage_ticks": Decimal(0),
        "candles": dip_then_rally(),
    }
    kwargs.update(overrides)
    return execute_backtest(**kwargs)  # type: ignore[arg-type]


def test_a_crossover_series_produces_a_trade_and_coherent_metrics() -> None:
    trades, metrics = run_it()
    assert metrics.total_trades == len(trades)
    assert metrics.total_trades >= 1
    # Reconciliation is the engine's own invariant; here we only assert the metrics summarise the
    # same trades the runner returned.
    assert metrics.long_trades + metrics.short_trades == metrics.total_trades


def test_the_window_excludes_candles_outside_the_dates() -> None:
    """A date range that ends before the series starts yields no candles — a valid, empty run."""
    trades, metrics = run_it(date_from=START - 10 * HOUR, date_to=START - 5 * HOUR)
    assert trades == []
    assert metrics.total_trades == 0


def test_a_spread_cost_model_eats_into_the_result() -> None:
    """The same run with a spread nets less than costless — proof the cost model is wired, not
    ignored."""
    _, costless = run_it(cost_model={"type": "none"})
    _, spread = run_it(cost_model={"type": "spread", "spread_points": 20})
    assert spread.net_profit < costless.net_profit


def test_an_unknown_cost_model_raises() -> None:
    with pytest.raises(ValueError, match="unknown cost model type"):
        run_it(cost_model={"type": "teleport"})


def test_an_unknown_timeframe_raises() -> None:
    with pytest.raises(ValueError, match="timeframe"):
        run_it(timeframe="Z9")


def test_a_strategy_without_percent_risk_sizing_raises() -> None:
    definition = ma_cross()
    definition["risk"] = {}
    with pytest.raises(ValueError, match="percent_risk sizing"):
        run_it(definition=definition)
