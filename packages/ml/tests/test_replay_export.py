"""The independent base: the real MM9 setups, replayed over real bars, against a real run."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import SpreadCostModel
from tradeforge_engine.loop import run
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import ARMS_A_RESTING_LIMIT, EURUSD, HOUR, FixedRisk
from tradeforge_ml.export import EVENT_SCHEMA
from tradeforge_ml.replay import costs_from, replay
from tradeforge_ml.replay_export import WARM_BARS, Job, _window, candles_of, write_independent

BARS = list(ARMS_A_RESTING_LIMIT)
"""175 measured EURUSD H1 bars (the engine's own test market)."""

PARAMS: dict[str, dict[str, Any]] = {
    "mme9_pullback": {"corrections": 1},
    "mme9_breakout": {"gift_stop": "gift", "entry_point": "classic", "volume_filter": False},
    "mme9_turn": {},
    "mme9_failed_turn": {},
}


def document(kind: str, side: str = "both") -> dict[str, Any]:
    """An unmanaged MM9 document, as a sweep's catalogue writes it."""
    params = {
        "side": side,
        "period": 9,
        "breakeven_at_r": None,
        "stop_buffer_ticks": 0,
        "long_average_period": None,
        **PARAMS[kind],
    }
    return {
        "exit": {"take_profit": None},
        "name": f"test {kind}",
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1}}},
        "setup": {"type": kind, "params": params},
        "timeframe": "H1",
        "schema_version": "1.0",
    }


COSTS = {"type": "spread", "spread_points": "10"}


@pytest.mark.parametrize("kind", sorted(PARAMS))
def test_until_a_run_is_first_blocked_the_replay_takes_its_very_entries(kind: str) -> None:
    """Flat, a run and the replay are the same thing: the first entry is identical, and the replay
    proposes at least as many."""
    ran = run(
        candles=BARS,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=compile_strategy(document(kind)),
        broker=BacktestBroker(instrument=EURUSD, cost_model=costs_from(COSTS)),
        risk=FixedRisk(),
        record_snapshots=False,
    )
    # A short barrier, so the replay's trades close inside these few bars: one left open at the
    # end is left out, and the first comparison would then be against the second.
    proposed = replay(
        strategy=compile_strategy(document(kind)),
        candles=BARS,
        instrument=EURUSD,
        cost_model=costs_from(COSTS),
        horizon=20,
    )

    assert ran.trades, "the measured market must give this setup a trade"
    first, mine = ran.trades[0], proposed[0].trade
    assert (mine.entry_time, mine.side, mine.entry_price, mine.stop_loss) == (
        first.entry_time,
        first.side,
        first.entry_price,
        first.stop_loss,
    )
    assert len(proposed) >= len(ran.trades)


def test_the_window_is_the_run_and_the_warm_bars_before_it() -> None:
    bars = [*BARS] * 1  # 175 hourly bars
    job = Job(
        run_id="r",
        entry_id="e",
        symbol="EURUSD",
        timeframe="H1",
        configuration={},
        definition={},
        instrument={},
        costs=COSTS,
        date_from=bars[150].time,
        date_to=bars[160].time,
    )

    window = _window(bars, job)

    assert window[-1].time == bars[160].time
    assert window[0].time == bars[max(0, 150 - WARM_BARS)].time


def test_a_charts_bars_are_read_with_their_spread(tmp_path: Path) -> None:
    chart = tmp_path / "symbol=EURUSD" / "timeframe=H1" / "year=2024"
    chart.mkdir(parents=True)
    part = BARS[:10]
    decimal = pa.decimal128(20, 10)
    pq.write_table(
        pa.table(
            {
                "time": pa.array([bar.time for bar in part], type=pa.timestamp("us", tz="UTC")),
                "open": pa.array([bar.open for bar in part], type=decimal),
                "high": pa.array([bar.high for bar in part], type=decimal),
                "low": pa.array([bar.low for bar in part], type=decimal),
                "close": pa.array([bar.close for bar in part], type=decimal),
                "tick_volume": [7] * 10,
                "spread": pa.array([12] * 10, type=pa.int32()),
            }
        ),
        chart / "part.parquet",
    )

    read = candles_of(tmp_path, "EURUSD", "H1")

    assert [bar.close for bar in read] == [bar.close for bar in part]
    assert {bar.spread for bar in read} == {12}
    assert candles_of(tmp_path, "GBPUSD", "H1") == []


def test_the_base_is_written_one_row_per_entry_with_the_configurations_that_took_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tradeforge_ml.replay_export.candles_of", lambda *_: BARS)
    instrument = {
        "symbol": "EURUSD",
        "name": "Euro",
        "asset_class": "forex",
        "currency_quote": "USD",
        "currency_base": "EUR",
        "tick_size": str(EURUSD.tick_size),
        "tick_value": str(EURUSD.tick_value),
        "contract_size": str(EURUSD.contract_size),
        "digits": 5,
        "exchange": None,
    }

    def job(run_id: str, configuration: dict[str, Any]) -> Job:
        return Job(
            run_id=run_id,
            entry_id="mm9",
            symbol="EURUSD",
            timeframe="H1",
            configuration=configuration,
            definition=document("mme9_turn"),
            instrument=instrument,
            costs=COSTS,
            date_from=BARS[0].time,
            date_to=BARS[-1].time,
        )

    # Two configurations that propose the very same entries: one event each, both named.
    report = write_independent(
        [job("a", {"x": 1}), job("b", {"x": 2})],
        sweep_id="s",
        out=tmp_path,
        ohlcv=tmp_path,
        horizon=20,
        processes=1,
    )

    base = tmp_path / "sweep=s" / "independent-h20"
    events = pq.read_table(base / "events.parquet")
    assert events.schema == EVENT_SCHEMA
    assert report.proposals == 2 * report.events
    assert set(events.column("n_configurations").to_pylist()) == {2}
    assert set(events.column("exit_reason").to_pylist()) <= {"sl", "time"}
    manifest = json.loads((base / "manifest.json").read_text())
    assert (manifest["kind"], manifest["horizon_bars"], manifest["events"]) == (
        "independent",
        20,
        report.events,
    )


def test_the_spread_costs_the_same_r_on_every_proposal() -> None:
    """R is per lot of risk: one lot or ten, a proposal's R is its own."""
    proposals = replay(
        strategy=compile_strategy(document("mme9_turn")),
        candles=BARS,
        instrument=EURUSD,
        cost_model=SpreadCostModel(spread_points=Decimal(10)),
        horizon=20,
    )

    for proposal in proposals:
        trade = proposal.trade
        assert trade.stop_loss is not None
        risk = abs(trade.entry_price - trade.stop_loss) / EURUSD.tick_size * EURUSD.tick_value
        assert trade.costs == pytest.approx(Decimal(10) * EURUSD.tick_value)  # half in, half out
        assert trade.r_multiple == pytest.approx(trade.net_pnl / risk, rel=Decimal("1e-6"))
        assert trade.entry_time - BARS[0].time >= dt.timedelta(0)
