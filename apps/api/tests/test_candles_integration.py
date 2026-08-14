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
from tradeforge_engine.testing import BULLISH_START, GAPPING_IMPULSE, bar

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


def _launch_with(
    client: TestClient,
    document: dict[str, Any],
    *,
    date_from: dt.datetime = START,
    date_to: dt.datetime = START + 100 * HOUR,
) -> str:
    created = client.post("/strategies", json=document)
    assert created.status_code == 201, created.text
    enqueued = client.post(
        "/backtests",
        json={
            "strategy_id": created.json()["id"],
            "symbol": "EURUSD",
            "timeframe": "H1",
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
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


def test_the_two_provenance_numbers_are_reported_when_they_disagree(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """A bar appearing *inside* the window after the run, which is the case the pair exists for.

    Both endpoints carry `candles_seen` (what the run recorded eating) and `count` (what is on
    disk for that window now), and until this test the only scenario exercised had them equal —
    so `count=read.seen` would have been indistinguishable from the truth. A field added to make
    a disagreement visible has to be seen disagreeing.

    A bar appearing in the middle is worse than one appended to the end, and that is why the hole
    is in the middle: an extra bar at the end would add a point to the curve, while an extra bar
    in the middle **reseeds the average and moves every point after it**. The candles endpoint
    would look almost right; the overlay would be a different line under the same trades.

    ⚠️ The second write passes all twelve bars. `write_candles` replaces whole year partitions,
    so writing only the missing hour would delete the other eleven.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    complete = [bar(i, open_=_LEVELS[i], close=_LEVELS[i + 1]) for i in range(len(_LEVELS) - 1)]
    # The same series with its sixth hour missing — the shape a gap in collection leaves behind.
    with_a_hole = [candle for index, candle in enumerate(complete) if index != 5]
    write_candles(tmp_path, "EURUSD", "H1", with_a_hole)

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, backtest_id)

        # The gap is filled in afterwards, the way a re-collection would fill it.
        write_candles(tmp_path, "EURUSD", "H1", complete)

        candles = client.get(f"/backtests/{backtest_id}/candles").json()
        overlays = client.get(f"/backtests/{backtest_id}/overlays").json()

    assert candles["candles_seen"] == len(with_a_hole)
    assert candles["count"] == len(complete)
    assert candles["candles_seen"] != candles["count"]
    # And the overlay reports the same disagreement, over the same window.
    assert overlays["candles_seen"] == candles["candles_seen"]
    assert overlays["count"] == candles["count"]


# --------------------------------------------------------------------------- #
# `/overlays` — the regions the strategy marked                                 #
# --------------------------------------------------------------------------- #

_TAKES_THE_SECONDARY = [
    bar(10, open_="123", close="121", high="124", low="120"),
    bar(11, open_="121", close="119", high="122", low="118"),
    bar(12, open_="119", close="118", high="120", low="116"),  # low 116 <= 117 -> takes it
    bar(13, open_="118", close="119", high="120", low="117"),
    bar(14, open_="119", close="120", high="121", low="118"),
    bar(15, open_="120", close="119", high="121", low="115"),  # a SECOND visit, deeper
    bar(16, open_="119", close="120", high="121", low="118"),
]
"""A pullback that reaches one region's entry edge and stops clear of the other.

`GAPPING_IMPULSE` leaves both its regions standing, which is only half of what a chart has to
draw. These bars dip to 116, under the secondary's top of 117 and nowhere near the primary's 100 —
so the same run serves one taken region and one still standing, and the two `mitigated_at` values
can be told apart within a single response.

⚠️ **Bar 15 goes deeper than bar 12, and that is the point of it.** The engine folds `mitigated`
forward with `or`, so a stamp written on the condition rather than on the transition would keep
being overwritten and land on 15. Verified by mutation: with `if reached:` in `_advance`, the
served `mitigated_at` moves from bar 12 to bar 15 and this file goes red. Without a second visit
the two implementations are indistinguishable here.

Probed before it was written, never derived on paper: driven through the real setup these bars add
**no** region of their own, so the scenario stays two zones wide and legible.
"""


def _structure_strategy() -> dict[str, Any]:
    """A CHoCH setup with secondaries on, which is what makes the scenario mark two regions.

    Left off, the pause's zone would never be offered and the primary/secondary distinction — a
    solid border against a dashed one on the chart — would have nothing to draw.
    """
    return {
        "schema_version": "1.0",
        "name": "CHoCH for the region chart",
        "timeframe": "H1",
        "setup": {"type": "structure_choch", "params": {"allow_secondary": True}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _structured_candles() -> list[Candle]:
    """A series that breaks structure and leaves regions behind — the author's own example.

    Three parts, and none of them optional. `BULLISH_START` is the toll: `MarketStructure` starts
    at `DIR = -1`, so a series that simply rises never leaves the starting gate and marks nothing.
    `GAPPING_IMPULSE` is the validated impulse that marks two demand regions. `_TAKES_THE_SECONDARY`
    comes back for one of them.
    """
    return [*BULLISH_START, *GAPPING_IMPULSE, *_TAKES_THE_SECONDARY]


def _launch_over_the_structure(
    client: TestClient, *, date_to: dt.datetime = START + 100 * HOUR
) -> str:
    """The window has to open before `START` — `BULLISH_START` sits in the hours in front of it."""
    return _launch_with(
        client,
        _structure_strategy(),
        date_from=START - len(BULLISH_START) * HOUR,
        date_to=date_to,
    )


def test_a_structure_setup_is_drawn_as_regions_and_a_swing_setup_as_curves(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The two halves of the overlay are independent, and each setup fills exactly one of them.

    Asserted together on purpose. Either half alone would pass against an endpoint that returned
    an empty overlay for everything — "the structure setup has no curves" is true of a response
    with nothing in it at all. It is the *pair* that says the two are populated by different
    strategies, which is the whole shape of the payload.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _structured_candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        structure_id = _launch_over_the_structure(client)
        _run(session_factory, tmp_path, structure_id)
        structure = client.get(f"/backtests/{structure_id}/overlays").json()

        swing_id = _launch_with(client, _setup_strategy())
        _run(session_factory, tmp_path, swing_id)
        swing = client.get(f"/backtests/{swing_id}/overlays").json()

    assert structure["zones"] != []
    assert structure["series"] == []
    assert swing["series"] != []
    assert swing["zones"] == []


def test_the_regions_served_are_the_ones_the_author_s_example_marks(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Two demand regions, primary and secondary, at the levels the engine's golden pins.

    The prices are asserted here as well as in `test_structure.py` because they are what reaches
    the browser: an endpoint that served a region's *origin* leg instead of its marking candle
    would still return two plausible rectangles at plausible prices, and only the numbers say
    which.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _structured_candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_over_the_structure(client)
        _run(session_factory, tmp_path, backtest_id)
        zones = client.get(f"/backtests/{backtest_id}/overlays").json()["zones"]

    # ⚠️ Compared as `Decimal`, never as the rendered text. The dataset's own scale rides along
    # (`100` arrives as `100.0000000000`), so asserting the literal would pin the number of
    # decimal places a Parquet column happens to carry inside an assertion about a *price*.
    assert [(z["kind"], Decimal(z["top"]), Decimal(z["bottom"]), z["primary"]) for z in zones] == [
        ("demand", Decimal(100), Decimal(98), True),
        ("demand", Decimal(117), Decimal(110), False),
    ]
    # Strings on the wire like every other price here — a float would round the rectangle's
    # edges, and an edge is where a limit order rests.
    assert all(isinstance(z["top"], str) and isinstance(z["bottom"], str) for z in zones)


def test_the_three_instants_of_a_region_do_not_collapse_into_one(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Marked, confirmed and taken are three different bars, and the chart reads all three.

    ⚠️ The `confirmed_at > from_time` assertion is the load-bearing one. Ordering alone
    (`>=`) is satisfied by a payload that served the same instant three times, and a fixture where
    every region happened to confirm on its own marking bar would sail through the whole loop
    proving nothing. Here the gap is real and large: both regions are drawn on bars the break that
    reveals them is still six and two hours away from.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _structured_candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_over_the_structure(client)
        _run(session_factory, tmp_path, backtest_id)
        zones = client.get(f"/backtests/{backtest_id}/overlays").json()["zones"]

    for zone in zones:
        marked, confirmed = _moment(zone["from_time"]), _moment(zone["confirmed_at"])
        assert confirmed > marked, zone
        if zone["mitigated_at"] is not None:
            assert _moment(zone["mitigated_at"]) >= marked, zone

    # And they are the bars the scenario says: both regions are revealed by the one break on
    # bar 9, having been drawn on bars 3 and 7.
    assert [_moment(z["from_time"]) for z in zones] == [START + 3 * HOUR, START + 7 * HOUR]
    assert [_moment(z["confirmed_at"]) for z in zones] == [START + 9 * HOUR] * 2


def test_a_region_still_standing_is_served_as_null_not_as_the_last_bar(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """`null` is "price never came back", and the chart draws it to its own right edge.

    Serving the final bar instead would be an assertion that price returned on the last candle of
    the run — and on a chart the two are indistinguishable, because a rectangle ending at the last
    bar is exactly where one extended to the edge ends up. Every region in the database would
    quietly read as taken.

    The taken one is asserted in the same breath, and has to be: `mitigated_at: None` for
    everything would pass the null half on its own.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    bars = _structured_candles()
    write_candles(tmp_path, "EURUSD", "H1", bars)

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_over_the_structure(client)
        _run(session_factory, tmp_path, backtest_id)
        zones = client.get(f"/backtests/{backtest_id}/overlays").json()["zones"]

    standing, taken = zones
    assert standing["mitigated_at"] is None
    # The first touch, not the deepest and not the last: bar 15 dips further under 117 than
    # bar 12 did. See `_TAKES_THE_SECONDARY`.
    assert _moment(taken["mitigated_at"]) == START + 12 * HOUR
    assert _moment(taken["mitigated_at"]) < bars[-1].time


def test_the_last_bar_of_the_window_is_replayed_like_every_other(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The test that separates a full replay from one that stops a bar short.

    Every other scenario here ends on a bar that marks nothing and takes nothing, so dropping it
    changes no answer — `read.candles[:-1]` survives all of them. This one ends on bar 12, the
    bar whose wick takes the secondary, so the region's death *is* the final candle.

    Both halves of the damage are real and neither shows on screen. A run stopping mid-pullback
    would serve `mitigated_at: null`, and a rectangle drawn to the right edge is exactly what a
    region that survived looks like. The mirror is worse: a break confirming on the last bar
    would mark regions that never reach the response at all, and nothing in the payload could
    say a region was missing.

    It also pins the window itself. The bars on disk run to bar 16; the run was launched to bar
    12, and the reply has to honour the provenance the run recorded rather than the dataset it
    can see — the same contract `/candles` is built on.
    """
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, "EURUSD", "H1", _structured_candles())

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        backtest_id = _launch_over_the_structure(client, date_to=START + 12 * HOUR)
        _run(session_factory, tmp_path, backtest_id)
        body = client.get(f"/backtests/{backtest_id}/overlays").json()

    _, taken = body["zones"]
    # Read on the run's very last candle, and therefore only reported by a replay that fed it.
    assert _moment(taken["mitigated_at"]) == START + 12 * HOUR
    assert body["candles_seen"] == len(BULLISH_START) + 13
