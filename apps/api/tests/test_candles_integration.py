"""`/backtests/{id}/candles` over a real Postgres and a real Parquet dataset.

The endpoint's whole contract is *provenance*: it serves the bars a finished run recorded
eating, not the bars its symbol happens to hold today. That difference cannot be tested with
a fake reader — it needs a dataset that changes underneath a run that already finished, which
is exactly what the first test here builds.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.worker import process_backtest
from tradeforge_collector import write_candles
from tradeforge_db.models import Backtest, BacktestStatus, Instrument
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.indicators import EMA
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)

_LEVELS = [
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


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any) -> None:
        self.jobs.append((function, args))


class _SilentRedis:
    """Stands in for Redis while the worker runs inline. Progress is not this file's subject."""

    async def publish(self, channel: str, message: str) -> None:
        return None


def _seed_instrument(session: Session) -> None:
    session.add(
        Instrument(
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
    )
    session.commit()


def _candles() -> list[Candle]:
    """Eleven consecutive hourly bars, starting at `START`."""
    return [bar(i, open_=_LEVELS[i], close=_LEVELS[i + 1]) for i in range(len(_LEVELS) - 1)]


def _moment(rendered: str) -> dt.datetime:
    """A timestamp off the wire, back as an instant.

    `fromisoformat` accepts the trailing `Z` from Python 3.11 on, which is what the API emits.
    """
    return dt.datetime.fromisoformat(rendered)


def _strategy() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "MA cross for the price chart",
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


def _app(settings: Settings, session_factory: Callable[[], Session], tmp_path: Path) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )


def _run(session_factory: Callable[[], Session], tmp_path: Path, backtest_id: str) -> None:
    """Invoke the worker inline, the way arq would have."""
    session = session_factory()
    try:
        asyncio.run(
            process_backtest(
                session=session,
                redis=_SilentRedis(),  # type: ignore[arg-type]
                parquet_root=tmp_path,
                backtest_id=uuid.UUID(backtest_id),
            )
        )
    finally:
        session.close()


def _launch(client: TestClient, hours: int = 100) -> str:
    """Create the strategy, enqueue a run over `hours` from `START`, and return its id."""
    created = client.post("/strategies", json=_strategy())
    assert created.status_code == 201, created.text
    enqueued = client.post(
        "/backtests",
        json={
            "strategy_id": created.json()["id"],
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": START.isoformat(),
            "date_to": (START + hours * HOUR).isoformat(),
            "initial_capital": "10000",
            "cost_model": {"type": "none"},
        },
    )
    assert enqueued.status_code == 202, enqueued.text
    return str(enqueued.json()["id"])


def test_the_chart_shows_what_the_run_read_even_after_the_dataset_grows(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The test the whole endpoint exists for.

    A run reads eleven bars. Afterwards the dataset is re-collected with eleven more hours on
    the end — which is an ordinary thing to happen, since a backfill is re-run as the market
    produces more history. The chart must still be the eleven bars the trades were made on.

    Serving the dataset instead would extend the chart into a period the strategy was never
    executed over: candles on the right with no trades on them, indistinguishable from a
    strategy that simply stopped taking signals. Nothing on screen would say the picture and
    the trades disagree.

    ⚠️ The second write passes the **old bars plus the new ones**. `write_candles` replaces
    whole year partitions, so writing only the new hours would delete the original eleven —
    and the test would pass for the wrong reason, with the endpoint returning a truncated
    window because the data was gone rather than because provenance bounded it.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    original = _candles()
    write_candles(tmp_path, "EURUSD", "H1", original)

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch(client)
        _run(session_factory, tmp_path, backtest_id)

        later = [
            bar(len(original) + i, open_="1.14000", close="1.14100") for i in range(len(original))
        ]
        write_candles(tmp_path, "EURUSD", "H1", original + later)

        response = client.get(f"/backtests/{backtest_id}/candles")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == len(original)
    assert body["candles_seen"] == len(original)
    # Instants, not the strings that spell them: the API renders UTC as `Z` and `isoformat`
    # writes `+00:00`, and a string comparison would be asserting a formatting choice.
    assert [_moment(candle["time"]) for candle in body["candles"]] == [
        candle.time for candle in original
    ]
    # The dataset is now twice as long, and none of the second half reached the response:
    # the window closes on the last bar the run ate, not on the last bar that exists.
    assert _moment(body["last_candle"]) == original[-1].time
    assert _moment(body["first_candle"]) == original[0].time
    assert later[0].time > original[-1].time


def test_the_bars_carry_the_prices_the_dataset_stored_exactly(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Prices cross the wire as strings, and the first bar is the one written.

    A JSON number would be an IEEE double, and the exact-decimal discipline the dataset keeps
    (`decimal128`, not `float64`) would be lost on the last hop to the chart that draws it.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch(client)
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/candles").json()

    first = body["candles"][0]
    written = _candles()[0]
    assert isinstance(first["open"], str)
    assert Decimal(first["open"]) == written.open
    assert Decimal(first["high"]) == written.high
    assert Decimal(first["low"]) == written.low
    assert Decimal(first["close"]) == written.close
    assert body["symbol"] == "EURUSD"
    assert body["timeframe"] == "H1"


def test_a_run_that_recorded_no_candles_is_refused_not_approximated(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """A failed run never reached a candle, so it has no window — and neither has any row
    written before `rev_0002`.

    The tempting fallback is `date_from`/`date_to`, which every such row still carries. It
    would always produce *a* chart, and that chart would be the period someone asked for
    rather than the period anything was measured over.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch(client)
        # Fail the run the way the worker would, leaving the provenance columns untouched.
        session = session_factory()
        try:
            run = session.get(Backtest, uuid.UUID(backtest_id))
            assert run is not None
            run.status = BacktestStatus.FAILED
            run.error = "the market went away"
            session.commit()
        finally:
            session.close()

        response = client.get(f"/backtests/{backtest_id}/candles")

    assert response.status_code == 404
    assert "did not record" in response.json()["detail"]


def test_a_run_too_long_to_draw_says_so_instead_of_truncating(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Over the cap the endpoint refuses, and the refusal names both numbers.

    It must not silently serve a prefix, and it must not quietly thin the bars out: a chart
    missing the high that hit a stop looks exactly like a chart that has it. The refusal is
    checked against `candles_seen`, so it costs one row read rather than a full scan.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch(client)
        _run(session_factory, tmp_path, backtest_id)
        # Claim a run far longer than it was. The provenance columns are what the guard reads,
        # so overstating them is enough — and it keeps the fixture from having to write 50 000
        # bars to Parquet to exercise a branch about not sending 50 000 bars.
        session = session_factory()
        try:
            run = session.get(Backtest, uuid.UUID(backtest_id))
            assert run is not None
            run.candles_seen = 50_001
            session.commit()
        finally:
            session.close()

        response = client.get(f"/backtests/{backtest_id}/candles")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "50001" in detail
    assert "50000" in detail


def test_an_unknown_backtest_is_a_404(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        response = client.get(f"/backtests/{uuid.uuid4()}/candles")

    assert response.status_code == 404
    assert response.json()["detail"] == "backtest not found"


# --------------------------------------------------------------------------- #
# `/overlays` — the curves the strategy was reading                             #
# --------------------------------------------------------------------------- #


def _setup_strategy() -> dict[str, Any]:
    """A `setup` document, which is what every strategy in this project's database actually is.

    Worth its own fixture beside the DSL one above: the two reach `overlays` by different paths,
    and the setup path is the one that would have been missed entirely by an implementation that
    read `definition["indicators"]` — a setup document has no such block.
    """
    return {
        "schema_version": "1.0",
        "name": "MME9 for the price chart",
        "timeframe": "H1",
        "setup": {"type": "mme9_breakout", "params": {"side": "long", "period": 3}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _launch_with(client: TestClient, document: dict[str, Any]) -> str:
    created = client.post("/strategies", json=document)
    assert created.status_code == 201, created.text
    enqueued = client.post(
        "/backtests",
        json={
            "strategy_id": created.json()["id"],
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": START.isoformat(),
            "date_to": (START + 100 * HOUR).isoformat(),
            "initial_capital": "10000",
            "cost_model": {"type": "none"},
        },
    )
    assert enqueued.status_code == 202, enqueued.text
    return str(enqueued.json()["id"])


def test_a_setup_reports_the_average_it_trades_even_with_no_indicators_block(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The test that would have caught the obvious wrong implementation.

    A `setup` document has no `indicators` key at all — the average lives inside the setup's own
    params. Every strategy in this project's database is one of these, so an overlay built by
    reading `definition["indicators"]` would have returned an empty list for all of them: a
    feature that ships, draws nothing, and raises no error anywhere.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/overlays").json()

    assert [series["label"] for series in body["series"]] == ["EMA 3"]
    assert body["symbol"] == "EURUSD"


def test_the_curve_starts_where_the_indicator_did_not_where_the_bars_do(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Warm-up bars are absent rather than null, so the series is shorter than the candles.

    Which is exactly why the points carry their own timestamps: a client joining curve to bars by
    *index* would draw every point one warm-up period to the left of where it belongs, and the
    shape would still look like a moving average.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    bars = _candles()
    write_candles(tmp_path, "EURUSD", "H1", bars)

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, backtest_id)
        overlays = client.get(f"/backtests/{backtest_id}/overlays").json()
        candles = client.get(f"/backtests/{backtest_id}/candles").json()

    [series] = overlays["series"]
    # A 3-period average: two bars of warm-up, so two fewer points than there are candles.
    assert len(series["points"]) == candles["count"] - 2
    assert _moment(series["points"][0][0]) > _moment(candles["candles"][0]["time"])
    # And it ends on the same bar the candles do — a curve stopping short would read as the
    # strategy having gone blind for the last stretch of the run.
    assert _moment(series["points"][-1][0]) == _moment(candles["candles"][-1]["time"])


def test_a_dsl_strategy_is_charted_under_the_ids_its_own_rules_refer_to(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The other path. Labels are the document's ids (`fast`, `slow`), not prettified names —
    those ids are what the conditions say, so the curve can be read against the rule."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch(client)
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/overlays").json()

    assert sorted(series["label"] for series in body["series"]) == ["fast", "slow"]


def test_prices_on_the_curve_are_strings_like_every_other_price(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/overlays").json()

    _, value = body["series"][0]["points"][0]
    assert isinstance(value, str)
    assert Decimal(value) > 0


def test_a_run_with_no_provenance_has_no_curves_either(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The same guard as `/candles`, and it has to be the same guard: a curve served for a window
    the candles endpoint refuses would be drawn against bars nothing agreed on."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        session = session_factory()
        try:
            run = session.get(Backtest, uuid.UUID(backtest_id))
            assert run is not None
            run.status = BacktestStatus.FAILED
            run.error = "never read a candle"
            session.commit()
        finally:
            session.close()

        response = client.get(f"/backtests/{backtest_id}/overlays")

    assert response.status_code == 404
    assert "did not record" in response.json()["detail"]


def test_every_value_on_the_curve_is_the_average_over_the_window_the_run_read(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The only test here that looks at the *numbers*.

    Every other one checks the shape of the response — the label, the length, the timestamps at
    the ends. A curve that was the right length, spanned the right period, and was made of
    different numbers would pass all of them.

    The expectation is built by driving the same indicator over the same bars under the engine's
    own decimal context. That deliberately shares the EMA implementation — this is not trying to
    re-derive an exponential average, which the engine's golden tests already pin — but it does
    not share the window, the seeding, the alignment or the context, and those are exactly what
    an endpoint recomputing a series can get wrong while still returning something plausible.

    **What it does not prove**, and the stronger anchor for it: a run *persists* the average it
    judged each entry against, into that trade's snapshot. Comparing the served curve to those
    recorded values would close the loop against numbers written during the run rather than
    beside it. It is not done here because this fixture produces no filled trade — on a series
    that climbs steadily the MME9 re-arms on every bar and the resting order is never taken — and
    inventing one is its own scenario. Noted in `specs/backlog.md`.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    bars = _candles()
    write_candles(tmp_path, "EURUSD", "H1", bars)

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/overlays").json()

    expected: list[tuple[dt.datetime, Decimal]] = []
    with localcontext(ENGINE_CONTEXT):
        indicator = EMA(period=3, source="close")
        for candle in bars:
            indicator.update(candle)
            value = indicator.value()
            if value is not None:
                expected.append((candle.time, value))

    [curve] = body["series"]
    served = [(_moment(when), Decimal(value)) for when, value in curve["points"]]

    assert served == expected
    # And the provenance travels with it, for the same reason it travels with the candles: one
    # extra bar inside the window does not add a point at the end, it reseeds the average and
    # moves the whole line.
    assert body["candles_seen"] == len(bars)
    assert body["count"] == len(bars)
