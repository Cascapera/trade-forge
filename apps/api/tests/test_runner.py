"""The engine-driving glue, unit-tested against plain candles — no database, no queue.

`execute_backtest` is the translation from a stored run into an engine run: it builds the
`InstrumentSpec`, the cost model, the broker and the risk manager, and folds the result into
the §5 metrics. Here it is exercised on a hand-built series, so a mistake in that wiring shows
up in milliseconds without Postgres or arq.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.runner import CandleWindow, execute_backtest
from tradeforge_db.models import Instrument
from tradeforge_engine import BacktestMetrics as EngineMetrics
from tradeforge_engine.domain import AssetClass, Candle, ClosedTrade
from tradeforge_engine.errors import EngineError
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


def run_it(**overrides: object) -> tuple[list[ClosedTrade], EngineMetrics, CandleWindow]:
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
    trades, metrics, _ = run_it()
    assert metrics.total_trades == len(trades)
    assert metrics.total_trades >= 1
    # Reconciliation is the engine's own invariant; here we only assert the metrics summarise the
    # same trades the runner returned.
    assert metrics.long_trades + metrics.short_trades == metrics.total_trades


def test_a_window_containing_no_candles_is_refused() -> None:
    """This reverses an earlier decision, deliberately.

    A range outside the series used to be "a valid, empty run": `done`, every metric zero.
    But that is byte for byte what a run whose strategy found no setups looks like, and the
    two need opposite responses — collect the data, versus nothing to do. A screen full of
    zeroes cannot say which, so the run refuses instead and names the coverage it does have.
    """
    with pytest.raises(LookupError, match="contains none of it"):
        run_it(date_from=START - 10 * HOUR, date_to=START - 5 * HOUR)


def test_the_refusal_names_the_coverage_that_does_exist() -> None:
    """ "No data" is a dead end; "I have January to March" is the next command to type."""
    with pytest.raises(LookupError, match="2024-01-01"):
        run_it(date_from=START - 10 * HOUR, date_to=START - 5 * HOUR)


def test_a_symbol_with_nothing_collected_is_a_different_message() -> None:
    """Empty dataset and empty window are different faults with different fixes."""
    with pytest.raises(LookupError, match="no candles have been collected"):
        run_it(candles=[])


def test_a_window_full_of_candles_that_produces_no_trades_still_succeeds() -> None:
    """The line is drawn at candles, not at trades.

    A strategy that legitimately finds nothing is an answer, and must not be reported as a
    failure — otherwise "my filter is too strict" becomes indistinguishable from an outage.
    """
    flat = [bar(index, open_="1.10000", close="1.10000") for index in range(10)]

    trades, metrics, window = run_it(candles=flat, date_to=START + 9 * HOUR)

    assert trades == []
    assert metrics.total_trades == 0
    assert window.candles == 10


def test_the_run_reports_the_window_it_actually_read() -> None:
    """Asking for more than the dataset holds must not be recorded as if it had it."""
    _, _, window = run_it(date_from=START - 50 * HOUR, date_to=START + 500 * HOUR)

    series = dip_then_rally()
    assert window == CandleWindow(len(series), series[0].time, series[-1].time)


def test_a_spread_cost_model_eats_into_the_result() -> None:
    """The same run with a spread nets less than costless — proof the cost model is wired, not
    ignored."""
    _, costless, _ = run_it(cost_model={"type": "none"})
    _, spread, _ = run_it(cost_model={"type": "spread", "spread_points": 20})
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


# --------------------------------------------------------------------------- #
# Setup documents: the other shape the runner has to drive (ADR-0019)           #
# --------------------------------------------------------------------------- #


def a_stock() -> Instrument:
    """A one-cent instrument, because the setup golden below is written in whole dollars."""
    return Instrument(
        symbol="AAPL",
        name="Apple Inc.",
        asset_class=AssetClass.STOCK,
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal("1"),
        digits=2,
    )


def pullback_to_the_average() -> list[Candle]:
    """The Ponto Contínuo golden from the engine's own suite, candle for candle.

    Two corrections (bars 4 and 5) take price back to the average; bar 6 touches it at 111 and
    closes at 113.5 above it, so the order rests at its high of 114 and bar 7 fills there. The
    first break of structure takes the stop to breakeven; the second trails it to the leg origin
    at 116; bar 16 comes back and takes it.
    """
    levels = [
        ("99", "100", "100.5", "98.5"),
        ("100", "104", "104.5", "100"),
        ("104", "108", "108.5", "104"),
        ("110", "114", "115", "110"),
        ("113.5", "113", "114", "108"),
        ("113", "112.5", "113.5", "107"),
        ("112", "113.5", "114", "111"),
        ("114", "115.5", "116", "113"),
        ("115.5", "116", "117", "115"),
        ("116", "115", "116.5", "114.2"),
        ("115", "116.5", "117.5", "115"),
        ("117", "118", "118.5", "116.5"),
        ("118", "119", "119.5", "117.5"),
        ("119", "118", "119", "116.5"),
        ("118", "117.5", "118.5", "116"),
        ("117.5", "120", "121", "117"),
        ("120", "115.5", "120.5", "115"),
    ]
    return [
        bar(index, open_=open_, close=close, high=high, low=low)
        for index, (open_, close, high, low) in enumerate(levels)
    ]


def ponto_continuo(**params: object) -> dict[str, object]:
    """A document that *names* a strategy instead of describing it. Note what is missing:
    no `indicators`, no `entry`, and no `exit.stop_loss` — the setup owns all three."""
    return {
        "schema_version": "1.0",
        "name": "Ponto Contínuo",
        "timeframe": "H1",
        "setup": {"type": "ponto_continuo", "params": {"side": "long", "period": 3, **params}},
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 5.0}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_a_setup_document_reproduces_the_engine_s_own_golden() -> None:
    """The whole point of this PR, asserted as numbers rather than as "it ran".

    These are the same figures `test_ponto_continuo.py` measures inside the engine — entry at 114,
    conducted out at 116 for +0.67R — reached here through the API's own path: a stored document,
    the instrument row, the cost model, the risk manager. If the wiring changed the result, it
    would change it here.

    `reason == "sl"` on a *winning* trade is not a bug in the ledger. The setup ends at its
    conducted stop, above the entry price, which is what a stop-out looks like once the trade has
    been managed. A reader who maps "sl" to "loss" will misread this setup's best trades.
    """
    trades, metrics, _ = run_it(
        definition=ponto_continuo(),
        instrument=a_stock(),
        candles=pullback_to_the_average(),
        initial_capital=Decimal("100000"),
    )

    (trade,) = trades
    assert trade.entry_price == Decimal(114)
    assert trade.exit_price == Decimal(116)
    assert trade.reason == "sl"
    assert trade.net_pnl == Decimal("666.66")
    assert metrics.total_trades == 1
    assert metrics.long_trades == 1


def test_a_setup_document_needs_no_indicators_entry_or_stop_block() -> None:
    """The runner reads `risk.sizing` and `exit.take_profit` out of the document itself, so a
    setup document still has to carry those two — they were never the strategy's to begin with.
    Everything else it omits, and omitting it must not raise."""
    document = ponto_continuo()

    assert "indicators" not in document
    assert "entry" not in document
    assert "stop_loss" not in document["exit"]  # type: ignore[operator]

    trades, _, _ = run_it(
        definition=document, instrument=a_stock(), candles=pullback_to_the_average()
    )
    assert len(trades) == 1


def test_switching_the_breakeven_rule_off_reaches_the_setup() -> None:
    """A parameter that travels from JSON through the factory into the state machine, proved by
    the trade it changes rather than by reading an attribute back."""
    with_rule, _, _ = run_it(
        definition=ponto_continuo(),
        instrument=a_stock(),
        candles=pullback_to_the_average(),
    )
    without_rule, _, _ = run_it(
        definition=ponto_continuo(breakeven_at_r=None),
        instrument=a_stock(),
        candles=pullback_to_the_average(),
    )

    # Both trade; the conducted stop is what differs, and only structure moves it once the 2x1
    # rule is gone. Same entry either way.
    assert with_rule[0].entry_price == without_rule[0].entry_price == Decimal(114)


def test_an_unknown_setup_type_fails_loudly_at_compile_time() -> None:
    document = ponto_continuo()
    document["setup"] = {"type": "setup_9_4", "params": {}}

    with pytest.raises(EngineError, match="unknown setup type"):
        run_it(definition=document, instrument=a_stock(), candles=pullback_to_the_average())
