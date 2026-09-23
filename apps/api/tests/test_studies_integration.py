"""`/studies` over a real Postgres: what the arithmetic tests deliberately cannot reach.

`test_studies.py` proves the dispersion arithmetic on in-memory rows, and `test_grid.py` proves
the expansion with no I/O at all. Everything *here* needs a database and an HTTP layer: that a
grid becomes N strategies, N runs and N jobs in one transaction; that a grid naming one bad path
writes **nothing**; that running the same grid twice does not collide on the strategy table; and
that the points come back placed on the grid rather than in whatever order Postgres felt like.

Run locally with `docker compose up -d`, then:

    POSTGRES_DB=tradeforge_test uv run pytest -m integration

⚠️ The variable is not optional, and since 2026-08-31 it is not merely asked for either:
`tradeforge_db.testing.truncate` refuses to empty a database whose name does not end in `_test`,
so a run pointed at the real one now stops in its fixture instead of emptying six tables. This
paragraph said the same thing on its own for months and the loss happened twice anyway — see
`packages/db/tests/test_truncate_guard_integration.py`.
"""

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import COLLECT_RANGE, RUN_BACKTEST
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.models import (
    Backtest,
    BacktestCollection,
    BacktestMetrics,
    BacktestStatus,
    Collection,
    Dataset,
    Instrument,
    Strategy,
    Study,
)
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
SYMBOL = "EURUSD"


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args))


def _seed_instruments(session: Session) -> None:
    for symbol in (SYMBOL, "GBPUSD"):
        session.add(
            Instrument(
                symbol=symbol,
                name=f"{symbol} for the study tests",
                asset_class=AssetClass.FOREX,
                currency_base=symbol[:3],
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
            )
        )
    session.commit()

    # ⚠️ **Coverage rows, and without them every launch below is refused** (PR-272): a study asks
    # the `datasets` index before queueing, as the sweep has since its first real run lost nine of
    # twelve runs to a window the data did not reach. Wide enough that `_body`'s window sits inside
    # it, so a test about something else is about that.
    for instrument in session.scalars(select(Instrument)):
        session.add(
            Dataset(
                instrument_id=instrument.id,
                timeframe="H1",
                date_from=START - dt.timedelta(days=365),
                date_to=START + dt.timedelta(days=365),
                candle_count=10_000,
                parquet_path=f"{instrument.symbol}/H1",
            )
        )
    session.commit()


def _strategy() -> dict[str, Any]:
    """A `setup` document, because that is what every strategy in this project's database is —
    and because its parameters are the ones a grid over this method would actually vary."""
    return {
        "schema_version": "1.0",
        "name": "MME9 for the grid",
        "timeframe": "H1",
        "setup": {
            "type": "mme9_breakout",
            "params": {"side": "long", "period": 9, "breakeven_at_r": 2.0},
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _app(settings: Settings, session_factory: Callable[[], Session], tmp_path: Path) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )


def _body(strategy_id: str, **overrides: Any) -> dict[str, Any]:
    """A 3x2 grid. Asymmetric on purpose: a square one is the shape where several wrong
    expansions agree with the right one — see `test_grid`."""
    return {
        "strategy_id": strategy_id,
        "symbol": SYMBOL,
        "timeframe": "H1",
        "date_from": START.isoformat(),
        "date_to": (START + 100 * HOUR).isoformat(),
        "initial_capital": "10000",
        "cost_model": {"type": "none"},
        "grid": {
            "setup.params.period": [5, 9, 20],
            "setup.params.breakeven_at_r": [1.0, 2.0],
        },
        **overrides,
    }


def _counts(session_factory: Callable[[], Session]) -> tuple[int, int, int]:
    """How many strategies, studies and runs exist right now."""
    session = session_factory()
    try:
        return (
            session.scalar(select(func.count()).select_from(Strategy)) or 0,
            session.scalar(select(func.count()).select_from(Study)) or 0,
            session.scalar(select(func.count()).select_from(Backtest)) or 0,
        )
    finally:
        session.close()


def _launch(client: TestClient, **overrides: Any) -> dict[str, Any]:
    created = client.post("/strategies", json=_strategy())
    assert created.status_code == 201, created.text
    response = client.post("/studies", json=_body(created.json()["id"], **overrides))
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_grid_becomes_one_strategy_one_run_and_one_job_per_combination(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Three periods by two breakeven levels is six of everything, written in one transaction.

    The job count is asserted alongside the rows because they are what makes a study *run*: a
    study that wrote six rows and queued five would sit forever with one point permanently
    `queued`, and nothing in the response would say so.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    app = _app(settings, session_factory, tmp_path)
    with TestClient(app) as client:
        body = _launch(client)

    assert len(body["points"]) == 6
    # The base strategy plus one per point.
    strategies, studies, runs = _counts(session_factory)
    assert (strategies, studies, runs) == (7, 1, 6)
    assert len(app.state.arq_pool.jobs) == 6


def test_a_grid_over_the_target_stores_one_document_with_it_and_one_without(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """His ask (22/09): search 2R against 3R against **no target**, in one study.

    The axis names a leaf (`exit.take_profit.params.rr`) that may not exist — "no target" is not
    an `rr` of anything, it is `take_profit: null`. This is the whole path: the grid goes over
    HTTP, the server expands it, and what is *stored* is checked, because the document is what
    the worker will run and the point's label is only a caption.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    # ⚠️ No `exit.stop_loss`: a setup places its own stop from the reference bar, and the DSL
    # refuses a document that names a second one (`semantic.py`). A target beside it is legal
    # precisely because of that — the risk it multiplies is the setup's.
    no_target = {**_strategy(), "exit": {"take_profit": None, "conditions": []}}
    app = _app(settings, session_factory, tmp_path)
    with TestClient(app) as client:
        created = client.post("/strategies", json=no_target)
        assert created.status_code == 201, created.text
        launched = client.post(
            "/studies",
            json=_body(created.json()["id"], grid={"exit.take_profit.params.rr": [2.0, None]}),
        )

    assert launched.status_code == 202, launched.text
    body = launched.json()
    # Labelled by the multiple, not by the block — what naming the leaf buys.
    assert [point["label"] for point in body["points"]] == ["rr=2.0", "rr=None"]

    session = session_factory()
    try:
        stored = session.scalars(
            select(Strategy).where(
                Strategy.id.in_([point["strategy_id"] for point in body["points"]])
            )
        ).all()
        targets = sorted(
            (one.definition["exit"]["take_profit"] for one in stored),
            key=lambda target: target is not None,
        )
    finally:
        session.close()

    assert targets == [None, {"type": "risk_multiple", "params": {"rr": 2.0}}]


def test_every_point_runs_its_own_document_not_one_shared_strategy(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The claim that makes a study reproducible, stated against the database.

    Each run points at a `strategy_id`, and *that document* is what it executed. Six runs
    sharing one strategy would still produce six rows and six results — and every one of them
    would be the same backtest, with the run log presenting them as a search of the space.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        body = _launch(client)

    session = session_factory()
    try:
        periods = sorted(
            session.get(Strategy, uuid.UUID(point["strategy_id"])).definition["setup"]["params"][  # type: ignore[union-attr]
                "period"
            ]
            for point in body["points"]
        )
    finally:
        session.close()

    assert periods == [5, 5, 9, 9, 20, 20]


def test_the_same_grid_launched_twice_reuses_its_strategies_instead_of_colliding(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ Without this the second study fails outright, and the failure is a 500.

    A point's document holds no symbol and no dates — those live on the run — so the same grid
    over a different market produces byte-identical documents under identical names. Written
    blindly they collide on the unique `(name, version)`.

    A *different* market is used on purpose: relaunching over the same one would be a plausible
    thing to forbid, while this is the case a study is for — the same parameter search carried
    to another instrument, which is precisely what a basket does one axis over.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        first = _launch(client)
        second_response = client.post(
            "/studies", json=_body(_base_id(session_factory), symbol="GBPUSD")
        )

    assert second_response.status_code == 202, second_response.text
    second = second_response.json()

    assert {point["strategy_id"] for point in second["points"]} == {
        point["strategy_id"] for point in first["points"]
    }
    # Still seven strategies: the base and the six points, written once and pointed at twice.
    strategies, studies, runs = _counts(session_factory)
    assert (strategies, studies, runs) == (7, 2, 12)


def _base_id(session_factory: Callable[[], Session]) -> str:
    """The id of the base strategy, read back out of the database."""
    session = session_factory()
    try:
        row = session.scalar(select(Strategy).where(Strategy.name == "MME9 for the grid"))
        assert row is not None
        return str(row.id)
    finally:
        session.close()


def test_a_grid_naming_a_path_this_strategy_lacks_writes_nothing_at_all(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """Refused whole, and the database is checked rather than trusted.

    A study that half-exists is worse than one that was refused: the caller asked one question
    about a space, and four runs plus an error answers a question nobody asked. The counts are
    compared before and after, because "the endpoint returned 422" says nothing about whether
    it wrote rows on the way there.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = client.post("/strategies", json=_strategy())
        before = _counts(session_factory)
        response = client.post(
            "/studies",
            json=_body(created.json()["id"], grid={"setup.params.periodd": [5, 9]}),
        )

    assert response.status_code == 422
    assert "nothing at" in response.json()["detail"]
    assert _counts(session_factory) == before


def test_a_grid_whose_values_make_no_runnable_strategy_writes_nothing_at_all(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The other refusal, and it is a different one: the path is fine and the *value* is not.

    A period of zero reaches a document the grid could apply and the DSL will not accept. With
    **every** point like that there is nothing to run, and the study is refused in the
    validator's own body — nothing written, nothing queued.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = client.post("/strategies", json=_strategy())
        before = _counts(session_factory)
        response = client.post(
            "/studies",
            json=_body(created.json()["id"], grid={"setup.params.period": [0]}),
        )

    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "strategy failed schema validation"
    assert _counts(session_factory) == before


def test_a_point_that_cannot_run_is_left_out_and_the_rest_runs(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ His request (18/09): a point the DSL refuses is dropped, not a reason to refuse the grid
    — the sweep's rule, now the study's. Period 0 cannot run; 9 and 20 can, and only they are
    written and queued."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    app = _app(settings, session_factory, tmp_path)
    with TestClient(app) as client:
        created = client.post("/strategies", json=_strategy())
        response = client.post(
            "/studies",
            json=_body(created.json()["id"], grid={"setup.params.period": [9, 0, 20]}),
        )

    assert response.status_code == 202, response.text
    labels = sorted(point["label"] for point in response.json()["points"])
    assert labels == ["period=20", "period=9"]
    # The base strategy plus the two points that run; one study; two runs, two jobs.
    assert _counts(session_factory) == (3, 1, 2)
    assert len(app.state.arq_pool.jobs) == 2


def test_a_higher_timeframe_no_coarser_than_the_chart_is_left_out(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The shape he asked about (18/09): the higher-timeframe axis swept across every chart,
    over a study at H1. M30 is finer than H1 — the DSL's rule refuses it — so that point is left
    out; H4 and D1 run. The preview names the one dropped, and the launch agrees with it."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()
    filtered = {
        "schema_version": "1.0",
        "name": "CHoCH under a higher timeframe",
        "timeframe": "H1",
        "setup": {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": 3}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    grid = {"setup.params.htf": ["M30", "H4", "D1"]}

    app = _app(settings, session_factory, tmp_path)
    with TestClient(app) as client:
        created = client.post("/strategies", json=filtered)
        assert created.status_code == 201, created.text
        strategy_id = created.json()["id"]
        preview = client.post(
            "/studies/preview", json={"strategy_id": strategy_id, "grid": grid, "timeframe": "H1"}
        ).json()
        response = client.post("/studies", json=_body(strategy_id, grid=grid))

    (dropped,) = preview["refusals"]
    assert dropped["values"] == {"setup.params.htf": "M30"}
    assert response.status_code == 202, response.text
    ran = sorted(point["values"]["setup.params.htf"] for point in response.json()["points"])
    assert ran == ["D1", "H4"]


def test_the_points_come_back_placed_on_the_grid_not_in_the_order_postgres_returns_them(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The bug this endpoint had, and it is invisible unless the order is asserted.

    `created_at` defaults to `now()`, which in Postgres is the **transaction's** start — so
    every run of one study carries the identical timestamp and the tiebreaker falls to a random
    UUID. Ordering by it is ordering by nothing.

    ⚠️ **One run is marked `running` before the read, and without that this test is vacuous.**
    Measured: straight after the insert an unordered scan returns the rows in grid order
    (`5, 5, 9, 9, 20, 20`) purely because that is the order they were written to the heap — so
    deleting the sort entirely leaves the test green. After a single `UPDATE` the same scan
    returns `5, 9, 9, 20, 20, 5`, because Postgres rewrites an updated tuple at the end of the
    heap. That is not an exotic state: a run is updated when it starts, again when it finishes,
    and again when its metrics land, so by the time anyone reads a study the order is scrambled
    for certain. The instant this test originally captured — nothing run yet — is the one
    instant in a study's life when the bug is invisible.

    The order asserted is the grid's own, last axis varying fastest, which is what a heatmap
    laid out row by row is reading.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = _launch(client)

        session = session_factory()
        try:
            # ⚠️ The **first point of the grid**, named by its id — not `select(Backtest).first()`,
            # which returns whatever the scan hands over and made this test pass or fail by
            # chance depending on which row it happened to pick. Naming the first point is what
            # makes the scramble deterministic: measured over three runs, the scan goes from
            # `5, 5, 9, 9, 20, 20` to `5, 9, 9, 20, 20, 5` every time.
            started = session.get(Backtest, uuid.UUID(created["points"][0]["backtest_id"]))
            assert started is not None
            started.status = BacktestStatus.RUNNING
            session.commit()
        finally:
            session.close()

        read_back = client.get(f"/studies/{created['id']}").json()

    coordinates = [
        (point["values"]["setup.params.period"], point["values"]["setup.params.breakeven_at_r"])
        for point in read_back["points"]
    ]

    assert coordinates == [(5, 1.0), (5, 2.0), (9, 1.0), (9, 2.0), (20, 1.0), (20, 2.0)]
    # And the two lists are parallel, so a client may read a row from either.
    assert len(read_back["runs"]) == len(read_back["points"])


def test_a_point_is_labelled_the_same_way_whether_it_is_created_or_read_back(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """One field, one format. The two bodies were built independently and disagreed once —
    the creation body served `period=5, ...` while the read served the stored strategy's full
    name — and only a client trying to match them would ever have found out."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = _launch(client)
        read_back = client.get(f"/studies/{created['id']}").json()

    assert [point["label"] for point in read_back["points"]] == [
        point["label"] for point in created["points"]
    ]
    assert read_back["points"][0]["label"] == "period=5, breakeven_at_r=1.0"


def test_the_summary_names_its_best_point_the_way_the_points_are_named(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """⚠️ The third place a label is produced, and the one that got it wrong.

    `points[].label` and `aggregate.best_label` are two answers to "which combination is this",
    and a reader's whole use for the second is to find the first. Served as the *stored
    strategy's* name — `MME9 for the grid [period=20, …]` against `period=20, …` — they never
    match, and a screen highlighting the best point highlights nothing.

    Neither existing suite could see it. The unit tests hand `aggregate_points` a label and assert
    what comes back, which proves the function and says nothing about its caller; the test
    above compares the creation body with the read body and never looks at the summary. This
    one closes the loop by asserting the summary against the points in the **same** response.

    Metrics are written by hand because the runs are only queued here — the point is the
    naming, and waiting for six backtests to execute would test the worker instead.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = _launch(client)

        session = session_factory()
        try:
            # Ascending profits in grid order, so the best is the last point and the worst the
            # first — an arrangement where naming them backwards would also be visible.
            for at, point in enumerate(created["points"]):
                run = session.get(Backtest, uuid.UUID(point["backtest_id"]))
                assert run is not None
                run.status = BacktestStatus.DONE
                net = Decimal(at * 100 - 200)
                session.add(
                    BacktestMetrics(
                        backtest_id=run.id,
                        # The table enforces `net = gross_profit + gross_loss`, with the two
                        # signed. Writing a net that does not balance would be a row the engine
                        # can never produce, and the fixture would be describing an impossible
                        # run to make a point about naming.
                        net_profit=net,
                        gross_profit=max(net, Decimal(0)),
                        gross_loss=min(net, Decimal(0)),
                        total_trades=0,
                        long_trades=0,
                        short_trades=0,
                        win_rate=Decimal(0),
                        max_drawdown_abs=Decimal(0),
                        max_drawdown_pct=Decimal(0),
                        max_dd_duration_days=0,
                        # NOT NULL, and the column is honest to insist: a run that finished
                        # produced a curve, even a flat one. Empty is the shape of a run that
                        # took no trades, which is what these hand-written rows describe.
                        equity_curve=[],
                    )
                )
            session.commit()
        finally:
            session.close()

        read_back = client.get(f"/studies/{created['id']}").json()

    labels = [point["label"] for point in read_back["points"]]
    assert read_back["aggregate"]["best_label"] in labels
    assert read_back["aggregate"]["worst_label"] in labels
    # And they are the right ends of it, so the pair cannot pass by being named backwards.
    assert read_back["aggregate"]["best_label"] == labels[-1]
    assert read_back["aggregate"]["worst_label"] == labels[0]


def test_the_grid_is_served_back_because_it_cannot_be_recovered_from_the_runs(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The axes, in the order they were declared. A client laying out a heatmap needs them, and
    the only other place they survive is inside strategy names as text."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = _launch(client)
        read_back = client.get(f"/studies/{created['id']}").json()

    assert read_back["grid"] == {
        "setup.params.period": [5, 9, 20],
        "setup.params.breakeven_at_r": [1.0, 2.0],
    }
    assert list(read_back["grid"]) == ["setup.params.period", "setup.params.breakeven_at_r"]


def test_deleting_a_study_leaves_its_runs_standing(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """SET NULL, not CASCADE. The framing of an experiment is disposable, the measurements are
    not — "I am done looking at this grid" must never mean "destroy six backtests"."""
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = _launch(client)

    session = session_factory()
    try:
        session.delete(session.get(Study, uuid.UUID(created["id"])))
        session.commit()
        surviving = list(session.scalars(select(Backtest)))
    finally:
        session.close()

    assert len(surviving) == 6
    assert all(run.study_id is None for run in surviving)


def test_an_unknown_study_is_a_404(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        response = client.get(f"/studies/{uuid.uuid4()}")

    assert response.status_code == 404


def test_a_study_refuses_a_request_it_cannot_run_and_says_which_part(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> None:
    """The three refusals before the grid is even looked at, none of which had a test.

    They were the last uncovered lines of `create_study`, and the symbol one is the reason it
    matters: delete that check and the next line reads `instrument.id` off `None`, which is an
    `AttributeError` and therefore a **500** on an ordinary typo.

    Asserted with their messages rather than only their status, because all three are 422 and a
    test that checked the number alone would pass with any two of them deleted.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = client.post("/strategies", json=_strategy())
        strategy_id = created.json()["id"]
        before = _counts(session_factory)

        unknown_symbol = client.post("/studies", json=_body(strategy_id, symbol="NOPE"))
        bad_timeframe = client.post("/studies", json=_body(strategy_id, timeframe="H7"))
        # Past `date_to`, not equal to it: the rule is `date_to >= date_from`, so a window of
        # zero length is legal and would have passed while proving nothing.
        backwards = client.post(
            "/studies",
            json=_body(strategy_id, date_from=(START + 200 * HOUR).isoformat()),
        )
        missing_strategy = client.post("/studies", json=_body(str(uuid.uuid4())))

    assert unknown_symbol.status_code == 422
    assert unknown_symbol.json()["detail"] == "unknown symbol: NOPE"
    assert bad_timeframe.status_code == 422
    assert backwards.status_code == 422
    assert backwards.json()["detail"] == "date_to precedes date_from"
    assert missing_strategy.status_code == 404
    # None of the four wrote anything.
    assert _counts(session_factory) == before


@pytest.mark.parametrize("symbol", ["EURUSD.raw", "#AAPL", "US500.cash"])
def test_a_strange_but_legal_symbol_reaches_the_handler(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    symbol: str,
) -> None:
    """⚠️ The half of the NUL guard that keeps it from growing an opinion.

    Broker symbols are genuinely strange — this project's own collector has met `EURUSD.raw`
    and a broker whose whole tree is `05Stocks2`. A pattern invented to catch a fuzzer would
    refuse real instruments, and the refusal would look exactly like a validation rule someone
    meant.

    The assertion is the **message**, not the status: an unknown symbol and a malformed one are
    both 422 here, and only the body says which happened. `unknown symbol` is the handler
    talking, which is the proof that validation let it through.
    """
    seeding = session_factory()
    _seed_instruments(seeding)
    seeding.close()

    with TestClient(_app(settings, session_factory, tmp_path)) as client:
        created = client.post("/strategies", json=_strategy())
        response = client.post("/studies", json=_body(created.json()["id"], symbol=symbol))

    assert response.status_code == 422
    assert response.json()["detail"] == f"unknown symbol: {symbol}"


class TestTheDataHasToBeThere:
    """PR-272: a study asks the `datasets` index before queueing, as every other launch does.

    ⚠️ **One market, N runs.** The download is the sweep's shape with a single pair — one per
    missing window, linked to every point — and the answer "do not collect" is the single
    backtest's: there is no other market to fall back on, so an empty window is refused.
    """

    @pytest.fixture
    def client(
        self, session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
    ) -> Any:
        seeding = session_factory()
        _seed_instruments(seeding)
        seeding.close()
        self.app = _app(settings, session_factory, tmp_path)
        with TestClient(self.app) as opened:
            yield opened

    def never_collected(self, session_factory: Callable[[], Session]) -> None:
        with session_factory() as session:
            for dataset in session.scalars(
                select(Dataset).join(Instrument).where(Instrument.symbol == SYMBOL)
            ):
                session.delete(dataset)
            session.commit()

    def launched(self, client: Any, **overrides: Any) -> Any:
        created = client.post("/strategies", json=_strategy())
        assert created.status_code == 201, created.text
        return client.post("/studies", json=_body(created.json()["id"], **overrides))

    def waits(self, session_factory: Callable[[], Session]) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Each run of the study, mapped to the collections it waits for."""
        with session_factory() as session:
            out: dict[uuid.UUID, set[uuid.UUID]] = {
                run.id: set() for run in session.scalars(select(Backtest))
            }
            for link in session.scalars(select(BacktestCollection)):
                out[link.backtest_id].add(link.collection_id)
            return out

    def test_a_window_with_no_candles_is_refused_and_nothing_is_written(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """Without the question, all six points would be queued and each would learn in a
        worker what the index already knew."""
        self.never_collected(session_factory)

        refused = self.launched(client)

        assert refused.status_code == 422
        assert refused.json()["detail"] == (
            f"no candles in this window for {SYMBOL} H1 (never collected)"
        )
        # The base strategy only: no study, no points, no runs, no jobs.
        assert _counts(session_factory) == (1, 0, 0)
        assert self.app.state.arq_pool.jobs == []

    def test_told_to_collect_the_window_is_downloaded_once_for_every_point(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """⚠️ **The test this slice exists for.** Six points over one pair: a collection per run
        would fetch the same year six times. One, and all six runs wait for it."""
        self.never_collected(session_factory)

        created = self.launched(client, collect_missing=True)

        assert created.status_code == 202, created.text
        with session_factory() as session:
            (collection,) = session.scalars(select(Collection)).all()
        assert (collection.symbol, collection.timeframe) == (SYMBOL, "H1")
        waits = self.waits(session_factory)
        assert len(waits) == 6
        assert all(linked == {collection.id} for linked in waits.values())
        functions = [function for function, _ in self.app.state.arq_pool.jobs]
        assert functions.count(COLLECT_RANGE) == 1
        assert functions.count(RUN_BACKTEST) == 6

    def test_a_window_covered_in_part_waits_for_its_gap_when_told_to_collect(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """A month missing at the start of a two-month window: without the flag it runs over what
        exists, with it every point waits for the month. (Four days at an edge would be forgiven
        by `EDGE_SLACK`, which is why the gap is a month.)"""
        with session_factory() as session:
            for dataset in session.scalars(
                select(Dataset).join(Instrument).where(Instrument.symbol == SYMBOL)
            ):
                dataset.date_from = START + dt.timedelta(days=30)
            session.commit()
        window = {"date_to": (START + dt.timedelta(days=60)).isoformat()}

        created = self.launched(client, collect_missing=True, **window)

        assert created.status_code == 202, created.text
        with session_factory() as session:
            (collection,) = session.scalars(select(Collection)).all()
        assert collection.date_from <= START
        assert all(linked == {collection.id} for linked in self.waits(session_factory).values())

    def test_told_to_collect_a_covered_window_collects_nothing(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """The flag asks for what is missing, not for a download: nothing missing, an ordinary
        launch — no collection written, no run left waiting."""
        created = self.launched(client, collect_missing=True)

        assert created.status_code == 202, created.text
        with session_factory() as session:
            assert session.scalars(select(Collection)).all() == []
        assert all(linked == set() for linked in self.waits(session_factory).values())
        functions = [function for function, _ in self.app.state.arq_pool.jobs]
        assert COLLECT_RANGE not in functions

    def test_without_the_flag_a_window_covered_in_part_runs_at_once(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        with session_factory() as session:
            for dataset in session.scalars(
                select(Dataset).join(Instrument).where(Instrument.symbol == SYMBOL)
            ):
                dataset.date_from = START + dt.timedelta(days=30)
            session.commit()

        created = self.launched(client, date_to=(START + dt.timedelta(days=60)).isoformat())

        assert created.status_code == 202, created.text
        with session_factory() as session:
            assert session.scalars(select(Collection)).all() == []

    def test_told_to_collect_a_symbol_the_broker_does_not_list_is_refused(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """AAPL on 18/09, now through a study: nothing to fetch, nothing to read."""
        self.never_collected(session_factory)
        with session_factory() as session:
            replace_snapshot(
                session,
                [BrokerSymbolEntry(symbol="GBPUSD")],
                server="Tradeview-Demo",
                synced_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
            )
            session.commit()

        refused = self.launched(client, collect_missing=True)

        assert refused.status_code == 422
        with session_factory() as session:
            assert session.scalars(select(Collection)).all() == []

    def test_a_window_without_a_zone_is_refused(self, client: Any) -> None:
        """⚠️ The index compares instants; a naive one would reach it as a 500. Refused at the
        door, as every other launch's window has been since PR-262."""
        refused = self.launched(client, date_from="2024-01-01T00:00:00")

        assert refused.status_code == 422
        assert "timezone" in refused.text
