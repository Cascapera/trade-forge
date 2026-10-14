"""The independent base: the real MM9 setups, replayed over real bars, against a real run."""

import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
from tradeforge_ml.replay_export import (
    WARM_BARS,
    Job,
    _window,
    candles_of,
    choose_jobs,
    replay_job,
    write_independent,
)
from tradeforge_ml.replay_export import Costs as CostSource

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


INSTRUMENT = {
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


def job(
    run_id: str,
    configuration: dict[str, Any],
    *,
    costs: dict[str, Any] = COSTS,
    spread_ticks: str | None = "10",
) -> Job:
    """A run of `mme9_turn` on EURUSD H1 over `BARS`, its market measured at `spread_ticks`."""
    return Job(
        run_id=run_id,
        entry_id="mm9",
        symbol="EURUSD",
        timeframe="H1",
        configuration=configuration,
        definition=document("mme9_turn"),
        instrument=INSTRUMENT,
        costs=costs,
        date_from=BARS[0].time,
        date_to=BARS[-1].time,
        spread_ticks=spread_ticks,
    )


def test_the_base_is_written_one_row_per_entry_with_the_configurations_that_took_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tradeforge_ml.replay_export.candles_of", lambda *_: BARS)

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
    assert manifest["costs"] == "measured"
    charged = {"type": "spread_commission", "spread_points": "10", "commission_per_unit": "0"}
    assert manifest["charged"] == {"EURUSD H1": charged}


@pytest.mark.parametrize(("costs", "paid"), [("measured", 10), ("run", 0)])
def test_the_label_charges_the_markets_measured_spread_not_what_the_run_paid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, costs: CostSource, paid: int
) -> None:
    """A run charged nothing (five of the seventeen bases of 13/10): its label pays the spread."""
    monkeypatch.setattr("tradeforge_ml.replay_export.candles_of", lambda *_: BARS)

    write_independent(
        [job("free", {}, costs={"type": "none"})],
        sweep_id="s",
        out=tmp_path,
        ohlcv=tmp_path,
        horizon=20,
        processes=1,
        costs=costs,
    )

    events = pq.read_table(tmp_path / "sweep=s" / "independent-h20" / "events.parquet")
    assert events.num_rows > 0
    expected = float(paid * EURUSD.tick_value)  # half the spread in, half out, one lot
    assert events.column("costs").to_pylist() == pytest.approx([expected] * events.num_rows)


def test_a_market_without_a_measured_spread_refuses_the_base(tmp_path: Path) -> None:
    unmeasured = [job("a", {}, spread_ticks=None)]

    with pytest.raises(ValueError, match="no measured spread for: EURUSD"):
        write_independent(unmeasured, sweep_id="s", out=tmp_path, ohlcv=tmp_path, processes=1)
    assert not (tmp_path / "sweep=s").exists()


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


# --------------------------------------------------------------------------------------------
# One run per entry, managed or not (12/10)
# --------------------------------------------------------------------------------------------

SPEC = {"symbol": "EURUSD", "name": "Euro", "asset_class": "forex", "currency_quote": "USD",
        "currency_base": "EUR", "tick_size": str(EURUSD.tick_size),
        "tick_value": str(EURUSD.tick_value), "contract_size": str(EURUSD.contract_size),
        "digits": 5, "exchange": None}  # fmt: skip


def managed(kind: str, rr: float | None, breakeven: float | None) -> dict[str, Any]:
    """`document(kind)` with a target and a breakeven, as a grid with those axes writes it."""
    doc = document(kind)
    if rr is not None:
        doc["exit"]["take_profit"] = {"type": "risk_multiple", "params": {"rr": rr}}
    doc["setup"]["params"]["breakeven_at_r"] = breakeven
    return doc


def row(  # noqa: PLR0913 — one per column of the query a test varies
    run_id: str,
    *,
    rr: float | None,
    breakeven: float | None,
    period: int = 9,
    symbol: str = "EURUSD",
    timeframe: str = "H1",
) -> SimpleNamespace:
    """A row of `jobs_of`'s query: a run, its document and its point's coordinates."""
    doc = managed("mme9_turn", rr, breakeven)
    doc["setup"]["params"]["period"] = period
    return SimpleNamespace(
        id=run_id,
        entry_id="mm9",
        symbol=symbol,
        timeframe=timeframe,
        coordinates={
            "setup.params.period": period,
            "exit.take_profit.params.rr": rr,
            "setup.params.breakeven_at_r": breakeven,
            "timeframe": timeframe,
        },
        definition=doc,
        instrument_spec=SPEC,
        cost_model=COSTS,
        date_from=BARS[0].time,
        date_to=BARS[-1].time,
        default_spread_points=Decimal("9.0000000000"),
    )


def test_a_grid_with_no_unmanaged_point_still_gives_each_entry_once() -> None:
    """⚠️ Every CHOCH and CONTINUATION grid of 08/10 to 11/10: targets of 3, 5, 7 or 10R and a
    breakeven or none, never "no target". Those sweeps had nothing to replay."""
    rows = [
        row(f"{period}-{rr}-{be}", rr=rr, breakeven=be, period=period)
        for period in (9, 21)
        for rr in (3, 5)
        for be in (None, 2)
    ]

    jobs = choose_jobs(rows)

    assert [job.configuration["setup.params.period"] for job in jobs] == [9, 21]
    for job in jobs:
        assert job.definition["exit"]["take_profit"] is None, "the target reached the replay"
        assert job.definition["setup"]["params"]["breakeven_at_r"] is None


def test_the_unmanaged_run_stands_for_its_entry_wherever_it_comes() -> None:
    """A base written before this one replayed the unmanaged runs: their entries must come from
    the very same runs, even when a managed sibling is read first."""
    jobs = choose_jobs(
        [
            row("managed-a", rr=3, breakeven=2),
            row("unmanaged", rr=None, breakeven=None),
            row("managed-b", rr=5, breakeven=None),
        ]
    )

    assert [job.run_id for job in jobs] == ["unmanaged"]


def test_each_chart_and_each_row_once() -> None:
    jobs = choose_jobs(
        [
            row("h1", rr=3, breakeven=None),
            row("h1", rr=3, breakeven=None),  # the query's join can repeat a run
            row("m15", rr=3, breakeven=None, timeframe="M15"),
            row("gbp", rr=3, breakeven=None, symbol="GBPUSD"),
        ]
    )

    assert sorted(job.run_id for job in jobs) == ["gbp", "h1", "m15"]
    assert {job.spread_ticks for job in jobs} == {"9.0000000000"}


@pytest.mark.parametrize("kind", sorted(PARAMS))
def test_a_managed_run_replays_to_exactly_its_unmanaged_siblings_entries(kind: str) -> None:
    """The claim the choice rests on, on the real setups and real bars: a run managed with a 3R
    target and a 1R breakeven, standing for its entry, gives the same proposals, to the trade,
    as the unmanaged run of the same entry would."""

    def job(definition: dict[str, Any]) -> Job:
        (chosen,) = choose_jobs(
            [
                SimpleNamespace(
                    **{**vars(row("r", rr=None, breakeven=None)), "definition": definition}
                )
            ]
        )
        return chosen

    unmanaged = replay_job(job(document(kind)), BARS, horizon=20)
    standing_in = replay_job(job(managed(kind, rr=3, breakeven=1)), BARS, horizon=20)
    # And without taking it off: the replay never lets a target or a breakeven reach a trade.
    kept_on = replace(job(document(kind)), definition=managed(kind, rr=3, breakeven=1))

    assert unmanaged, "the scenario proposes nothing; it cannot compare anything"
    assert any(trade.mfe_r is not None and trade.mfe_r >= 3 for trade in unmanaged), (
        "no trade reaches the 3R target: the comparison could not see a target applied"
    )
    assert standing_in == unmanaged
    assert replay_job(kept_on, BARS, horizon=20) == unmanaged
