"""`/sweeps` over a real Postgres — the product a study and a basket each refuse to take.

`test_sweep.py` proves the arithmetic on plain dictionaries. What only exists here is everything
that needs a database: that N entries over M timeframes over K markets become exactly N·M·K runs
in one transaction, that a refused combination is dropped rather than enqueued to fail, that the
points a sweep writes stay out of the strategy picker, and that the coordinates come back by key
rather than by splitting a name.

Run locally with:
    docker compose up -d
    POSTGRES_DB=tradeforge_test uv run pytest -m integration

⚠️ **The database name is not decoration.** These fixtures TRUNCATE every table in
`TABLES_CHILD_FIRST` before each test, against whatever database the environment points
at — and `POSTGRES_DB` defaults to `tradeforge`, the real one. Running this file without
the override once emptied this project's own backtests.
"""

import csv
import datetime as dt
import io
import uuid
from collections.abc import Callable
from decimal import Decimal
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE, RUN_BACKTEST
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.models import (
    Backtest,
    BacktestCollection,
    BacktestMetrics,
    BacktestStatus,
    CatalogEntry,
    Collection,
    Dataset,
    ExitReason,
    Instrument,
    Recorded,
    Sweep,
    SymbolHistory,
    Trade,
)
from tradeforge_engine.domain import AssetClass, Side

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
CAPITAL = "10000"
SYMBOLS = ("EURUSD", "GBPUSD", "USDJPY")


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it.

    ⚠️ **The options are recorded, not swallowed, and that is the point of this fake.** Every
    other fake in this suite keeps only `(function, args)` — which makes `_job_id` invisible, and
    `_job_id` is the whole of this endpoint's idempotency claim: a request that dies partway
    through queueing two thousand jobs is retried rather than reconciled by hand. A fake that
    drops it agrees with the router that passes it and with the router that does not.
    """

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args, options))


@pytest.fixture
def queue() -> _CapturingQueue:
    return _CapturingQueue()


@pytest.fixture
def client(
    session_factory: Callable[[], Session],
    settings: Settings,
    tmp_path: Path,
    queue: _CapturingQueue,
) -> Any:
    seeding = session_factory()
    for symbol in SYMBOLS:
        seeding.add(
            Instrument(
                symbol=symbol,
                name=f"{symbol} for the sweep tests",
                asset_class=AssetClass.FOREX,
                currency_base=symbol[:3],
                currency_quote=symbol[3:],
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=Decimal("8"),
            )
        )
    seeding.commit()

    # ⚠️ **Coverage rows, and without them every launch below is refused.** That is not fixture
    # noise: it is the check this suite gained after the first real sweep lost nine runs of
    # twelve to a window the data did not reach. The dates are wide enough that the window in
    # `a_sweep_body` sits inside them, so a test that means to be about something else is.
    for symbol in SYMBOLS:
        instrument = seeding.scalars(select(Instrument).where(Instrument.symbol == symbol)).one()
        for timeframe in ("M15", "H1", "H4", "D1"):
            seeding.add(
                Dataset(
                    instrument_id=instrument.id,
                    timeframe=timeframe,
                    date_from=START - dt.timedelta(days=365),
                    date_to=START + dt.timedelta(days=365),
                    candle_count=10_000,
                    # Required, and pointing at nothing on purpose: these tests never read a
                    # candle. What they exercise is the index, which is the whole reason the
                    # coverage check is cheap enough to run before a launch (ADR-05).
                    parquet_path=f"{symbol}/{timeframe}",
                )
            )
    seeding.commit()
    seeding.close()
    app = create_app(
        # ⚠️ One worker, whatever the machine running the suite has in its `.env`: the preview's
        # time is divided by the workers, and a developer's `TRADEFORGE_WORKERS=6` failed the
        # estimate's test locally while CI, with no `.env`, passed it (24/09).
        settings=settings.model_copy(update={"parquet_root": tmp_path, "tradeforge_workers": 1}),
        session_factory=session_factory,
        arq_pool=queue,
    )
    with TestClient(app) as opened:
        yield opened


def a_document(name: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "M15",
        # `side` is required: a directional setup trades one side, and the two-sided version is
        # two of them. Spelled out rather than defaulted, like every other choice in this DSL.
        "setup": {
            "type": "mme9_breakout",
            "params": {"side": "long", "period": 9, "breakeven_at_r": 2.0},
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def a_filtered_document(name: str) -> dict[str, Any]:
    """Under an H4 filter, which is the shape that makes some timeframes illegal."""
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "M15",
        "setup": {"type": "structure_choch", "params": {"htf": "H4", "htf_offset": 3}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def an_entry(
    client: Any,
    *,
    name: str,
    grid: dict[str, list[Any]] | None = None,
    document: dict[str, Any] | None = None,
) -> str:
    doc = document if document is not None else a_document(f"doc {uuid.uuid4()}")
    created = client.post("/strategies", json=doc)
    assert created.status_code == 201, created.text
    body: dict[str, Any] = {"name": name, "strategy_id": created.json()["id"]}
    if grid is not None:
        body["grid"] = grid
    entry = client.post("/catalog", json=body)
    assert entry.status_code == 201, entry.text
    return str(entry.json()["id"])


def finish(session_factory: Callable[[], Session], run_id: str, net: int) -> None:
    """Mark a queued run done with a hand-written result.

    The runs are only queued in this suite — what is under test is how a sweep is read back,
    and waiting for real backtests would test the worker instead. The row still has to be one
    the engine could produce: the table enforces `net = gross_profit + gross_loss`.
    """
    session = session_factory()
    try:
        run = session.get(Backtest, uuid.UUID(run_id))
        assert run is not None
        run.status = BacktestStatus.DONE
        profit = Decimal(net)
        session.add(
            BacktestMetrics(
                backtest_id=run.id,
                net_profit=profit,
                gross_profit=max(profit, Decimal(0)),
                gross_loss=min(profit, Decimal(0)),
                total_trades=0,
                long_trades=0,
                short_trades=0,
                win_rate=Decimal(0),
                max_drawdown_abs=Decimal(0),
                max_drawdown_pct=Decimal(0),
                max_dd_duration_days=0,
                equity_curve=[],
            )
        )
        session.commit()
    finally:
        session.close()


def a_sweep_body(entries: list[str], symbols: list[str], timeframes: list[str]) -> dict[str, Any]:
    return {
        "entry_ids": entries,
        "symbols": symbols,
        "timeframes": timeframes,
        "date_from": START.isoformat(),
        "date_to": (START + 100 * HOUR).isoformat(),
        "initial_capital": CAPITAL,
        "cost_model": {"type": "none"},
    }


class TestTheProduct:
    def test_entries_add_while_timeframes_and_markets_multiply(
        self, client: Any, queue: _CapturingQueue
    ) -> None:
        # ⚠️ The arithmetic said out loud once: entries are **alternatives**, not an axis. Two
        # entries of three and one points, over two timeframes and three markets, is
        # (3 + 1) x 2 x 3 = 24 — not 3 x 1 x 2 x 3.
        swept = an_entry(
            client, name=f"swept {uuid.uuid4()}", grid={"setup.params.period": [5, 9, 21]}
        )
        plain = an_entry(client, name=f"plain {uuid.uuid4()}")

        launched = client.post(
            "/sweeps", json=a_sweep_body([swept, plain], list(SYMBOLS), ["M15", "H1"])
        )

        assert launched.status_code == 202, launched.text
        assert launched.json()["runs"] == 24
        # Every run enqueued, and each under its own id so a retry is idempotent.
        assert len(queue.jobs) == 24
        assert len({job[1][0] for job in queue.jobs}) == 24

        # ⚠️ **The job id is the run id, and this is the line that says so.** The assertion
        # above counts distinct *arguments*, which stays true of a router that passes no
        # `_job_id` at all — and then a retry of a half-queued sweep enqueues every run a second
        # time. arq deduplicates on the job id, so the claim lives in the option, not the count.
        assert {job[2].get("_job_id") for job in queue.jobs} == {job[1][0] for job in queue.jobs}

    def test_the_preview_promises_what_the_launch_delivers(self, client: Any) -> None:
        # The property that makes a preview worth having. Without this pair each number could
        # drift alone, and the one a person reads is the one that would be wrong.
        entry = an_entry(client, name=f"swept {uuid.uuid4()}", grid={"setup.params.period": [5, 9]})
        body = a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15", "H1", "H4"])

        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        )
        launched = client.post("/sweeps", json=body)

        assert preview.json()["runs"] == 12
        assert launched.json()["runs"] == 12

    def test_the_preview_says_how_long_the_runs_would_take(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """His call (22/09): the time beside the count, measured from this database's own runs."""
        entry = an_entry(client, name=f"timed {uuid.uuid4()}")
        body = a_sweep_body([entry], ["EURUSD"], ["H1", "M15"])
        asked = {k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")}

        # Nothing has finished here yet: no estimate, rather than a zero that reads as instant.
        assert client.post("/sweeps/preview", json=asked).json()["backtest_time"] is None

        # One finished run to measure by: 100 H1 bars (the window is 100 hours) in 200 s.
        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()
        [row] = client.get(f"/sweeps/{launched['id']}").json()["runs"]
        finish(session_factory, row["run"]["id"], 100)
        with session_factory() as session:
            run = session.get(Backtest, uuid.UUID(row["run"]["id"]))
            assert run is not None
            run.started_at = dt.datetime(2026, 9, 20, 12, tzinfo=dt.UTC)
            run.finished_at = run.started_at + dt.timedelta(seconds=200)
            session.commit()

        # 2 s a bar; the sweep asks for 100 H1 bars and 400 M15 bars — 1000 s, on one run.
        preview = client.post("/sweeps/preview", json=asked).json()
        assert preview["backtest_time"] == {"seconds": 1000.0, "based_on": 1}


class TestTheTimeframeIsRealHere:
    def test_each_timeframe_becomes_its_own_run(self, client: Any) -> None:
        entry = an_entry(client, name=f"plain {uuid.uuid4()}")

        launched = client.post(
            "/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["M15", "H1", "H4"])
        )
        read = client.get(f"/sweeps/{launched.json()['id']}").json()

        assert {run["run"]["timeframe"] for run in read["runs"]} == {"M15", "H1", "H4"}

    def test_a_filtered_entry_loses_the_timeframes_its_filter_cannot_cover(
        self, client: Any
    ) -> None:
        # ⚠️ The whole reason the timeframe is written **into** the document. This entry filters
        # by H4, so M15 and H1 are legal and H4 itself is not — the filter would no longer be
        # coarser than the chart. Swept across all three, the sweep runs two and reports why the
        # third cannot, rather than enqueuing a run that fails in a worker.
        entry = an_entry(
            client,
            name=f"filtered {uuid.uuid4()}",
            document=a_filtered_document(f"choch {uuid.uuid4()}"),
        )
        body = a_sweep_body([entry], ["EURUSD"], ["M15", "H1", "H4"])

        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()

        assert preview["runs"] == 2
        (refusal,) = preview["entries"][0]["refusals"]
        assert refusal["values"]["timeframe"] == "H4"
        assert "htf" in refusal["reason"]

        launched = client.post("/sweeps", json=body)
        assert launched.json()["runs"] == 2

    def test_the_two_timeframes_do_not_collide_on_a_name(self, client: Any) -> None:
        # Two documents that differ only in timeframe share every other byte. Without the
        # timeframe in the generated name they would share a name too — and (name, version) is
        # unique, so the second would collide rather than insert.
        entry = an_entry(client, name=f"plain {uuid.uuid4()}")

        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["M15", "H1"]))

        assert launched.status_code == 202, launched.text
        read = client.get(f"/sweeps/{launched.json()['id']}").json()
        assert len({run["run"]["strategy_id"] for run in read["runs"]}) == 2


class TestReadingItBack:
    def test_the_coordinates_come_back_by_key_not_by_splitting_a_name(self, client: Any) -> None:
        # ⚠️ The first draft recovered these by matching the entry's name as a prefix of the
        # strategy's. The names here are chosen so that would have been ambiguous: one entry's
        # name is a prefix of the other's, which is a thing a person naming `9.1` and
        # `9.1 com filtro` does on the first afternoon.
        stem = str(uuid.uuid4())[:8]
        short = an_entry(client, name=f"9.1 {stem}", grid={"setup.params.period": [5]})
        long = an_entry(client, name=f"9.1 {stem} com filtro", grid={"setup.params.period": [9]})

        launched = client.post("/sweeps", json=a_sweep_body([short, long], ["EURUSD"], ["M15"]))
        read = client.get(f"/sweeps/{launched.json()['id']}").json()

        by_entry = {run["entry_id"]: run for run in read["runs"]}
        assert set(by_entry) == {short, long}
        assert by_entry[short]["values"]["setup.params.period"] == 5
        assert by_entry[long]["values"]["setup.params.period"] == 9
        # The timeframe is a coordinate too, keyed rather than parsed out of the caption.
        assert by_entry[short]["values"]["timeframe"] == "M15"

    def test_a_removed_entry_leaves_the_sweep_readable(self, client: Any) -> None:
        # Removing a label is allowed by design (`rev_0017`). What it may cost is the label,
        # never the measurement — a finished sweep that went blank would be a result destroyed
        # by tidying up.
        entry = an_entry(client, name=f"doomed {uuid.uuid4()}")
        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["M15"]))

        # ⚠️ The removal is its own statement, not an expression inside the `assert`. Under
        # `python -O` asserts are stripped — the entry would never be deleted and this would go
        # on passing as a test of something else entirely.
        removed = client.delete(f"/catalog/{entry}")
        assert removed.status_code == 204

        read = client.get(f"/sweeps/{launched.json()['id']}")
        assert read.status_code == 200
        assert len(read.json()["runs"]) == 1
        # The section keeps its summary and loses only its heading. Null rather than a generated
        # strategy's name, which carries one point's label and would head the section as if the
        # whole entry were that point.
        (summary,) = read.json()["entries"]
        assert summary["entry_name"] is None
        assert summary["aggregate"]["points_total"] == 1

    def test_each_entry_is_summarised_on_its_own_in_the_order_it_was_asked(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # ⚠️ **Pooled, these six runs have a median of 1.5%; per entry they have 2.5% and -2%.**
        # Entries are alternatives, so the pooled figure is the median of two methods and
        # describes neither — which is why `points_total` (4 and 2, never 6) is asserted too.
        # zeta's 1 000 is there so its mean (4%) is not its median: without it the two coincide
        # and a mean would pass as well.
        #
        # The asked order is set against the two orders an implementation could plausibly fall
        # into instead. The runs come back ordered by strategy name — the query orders by
        # `created_at` first, but the whole sweep is one transaction and `now()` is its start,
        # so that column ties — and `entries` built by walking the rows would list `alpha`
        # first; `alpha` is also created first, so ordering by the shelf's `created_at` would
        # too. Only the request says `zeta` leads.
        tag = str(uuid.uuid4())[:8]
        plain = an_entry(client, name=f"alpha {tag}")
        swept = an_entry(client, name=f"zeta {tag}", grid={"setup.params.period": [5, 9]})
        launched = client.post(
            "/sweeps", json=a_sweep_body([swept, plain], ["EURUSD", "GBPUSD"], ["M15"])
        )
        assert launched.status_code == 202, launched.text
        address = f"/sweeps/{launched.json()['id']}"

        profits = {swept: iter([100, 200, 300, 1000]), plain: iter([-100, -300])}
        net_of: dict[str, int] = {}
        for row in client.get(address).json()["runs"]:
            net = next(profits[row["entry_id"]])
            net_of[row["run"]["id"]] = net
            finish(session_factory, row["run"]["id"], net)

        read = client.get(address).json()

        assert [one["entry_id"] for one in read["entries"]] == [swept, plain]
        by_entry = {one["entry_id"]: one["aggregate"] for one in read["entries"]}
        assert [by_entry[swept]["points_total"], by_entry[plain]["points_total"]] == [4, 2]
        assert Decimal(by_entry[swept]["median_return"]) == Decimal("0.025")
        assert Decimal(by_entry[plain]["median_return"]) == Decimal("-0.02")
        # ⚠️ The best is named so a reader can find it: the point label alone (`M15 · period=9`)
        # is two runs here, one per market, and only the symbol tells them apart.
        best = max(
            (row for row in read["runs"] if row["entry_id"] == swept),
            key=lambda row: net_of[row["run"]["id"]],
        )
        assert by_entry[swept]["best_label"] == f"{best['run']['symbol']} · {best['label']}"

    def test_reading_a_sweep_costs_the_same_however_many_runs_it_holds(
        self,
        client: Any,
        session_factory: Callable[[], Session],
        migrated_engine: Engine,
    ) -> None:
        """The N+1 guard of a read that a screen polls, stated as a property, not a number.

        `Backtest.metrics` lazy-loads by default and `list_item` touches it, so without the
        `selectinload` every run costs one more query — and each drags that run's equity curve
        out of Postgres to build a row that does not contain it. The body stays correct and only
        the clock moves, which is what makes the regression invisible; the sweep screen then
        pays it again every few seconds. The basket's read has the same guard.

        The runs are finished first, so the metrics exist and the lazy path would have rows to
        drag. Two sweeps of two and three runs are compared with each other, and nothing is
        claimed about the absolute count, which would encode today's implementation.
        """
        entry = an_entry(client, name=f"counted {uuid.uuid4()}")
        sweep_ids: list[str] = []
        for symbols in (["EURUSD", "GBPUSD"], list(SYMBOLS)):
            launched = client.post("/sweeps", json=a_sweep_body([entry], symbols, ["M15"]))
            assert launched.status_code == 202, launched.text
            sweep_ids.append(launched.json()["id"])
        for sweep_id in sweep_ids:
            for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]:
                finish(session_factory, row["run"]["id"], 100)

        counted: list[int] = []

        def count(*_args: object, **_kwargs: object) -> None:
            counted[-1] += 1

        event.listen(migrated_engine, "before_cursor_execute", count)
        try:
            for sweep_id in sweep_ids:
                counted.append(0)
                read = client.get(f"/sweeps/{sweep_id}")
                assert read.status_code == 200
        finally:
            event.remove(migrated_engine, "before_cursor_execute", count)

        two_runs, three_runs = counted
        assert two_runs > 0, "the listener saw nothing, so this proves nothing"
        assert two_runs == three_runs, (
            f"a two-run sweep took {two_runs} queries and a three-run sweep took {three_runs}: "
            f"the per-row query is back, and with it the curve nobody reads"
        )

    def test_the_dataset_is_one_row_per_run_and_its_dictionary_names_every_column(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # The file and its legend over HTTP. `test_sweep_dataset.py` proves the rows on objects;
        # what only exists here is the join — the coordinates written at launch reaching the
        # grid columns, and the dictionary served for exactly the columns this file has.
        entry = an_entry(client, name=f"data {uuid.uuid4()}", grid={"setup.params.period": [5, 9]})
        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15"]))
        assert launched.status_code == 202, launched.text
        address = f"/sweeps/{launched.json()['id']}"
        first = client.get(address).json()["runs"][0]
        finish(session_factory, first["run"]["id"], 250)

        served = client.get(f"{address}/dataset.csv")
        dictionary = client.get(f"{address}/dataset/dictionary")

        assert served.status_code == 200, served.text
        assert served.headers["content-type"].startswith("text/csv")
        assert "attachment" in served.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(served.text)))
        assert len(rows) == 4
        assert dictionary.status_code == 200, dictionary.text
        assert [column["name"] for column in dictionary.json()["columns"]] == list(rows[0])
        assert sorted(row["param:setup.params.period"] for row in rows) == ["5", "5", "9", "9"]
        (done,) = [row for row in rows if row["status"] == "done"]
        assert done["run_id"] == first["run"]["id"]
        assert Decimal(done["return"]) == Decimal("0.025")
        # The three still queued keep their rows, with nothing where a result would be.
        assert {row["return"] for row in rows if row["status"] != "done"} == {""}

    def test_the_dataset_of_an_unknown_sweep_is_a_404(self, client: Any) -> None:
        missing = uuid.uuid4()

        served = client.get(f"/sweeps/{missing}/dataset.csv")
        dictionary = client.get(f"/sweeps/{missing}/dataset/dictionary")

        assert served.status_code == 404
        assert dictionary.status_code == 404


def a_zone_document(name: str) -> dict[str, Any]:
    """His CHOCH, with the three dials only some entry points read written out for a grid."""
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "M15",
        "setup": {
            "type": "structure_choch",
            "params": {
                "entry_point": "edge",
                "stop_buffer": 0.1,
                "gift_stop": "gift",
                "volume_filter": False,
            },
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


class TestPointsThatRunTheSameShareOneRun:
    """His answer, 24/09: `stop_buffer` under `martelo` is three points and one behaviour, so it is
    run once and the other two point at that run — on the screen as its equivalents, in the
    dataset as rows of their own."""

    GRID: dict[str, list[Any]] = {  # noqa: RUF012 — read-only, a class-level fixture
        "setup.params.entry_point": ["edge", "martelo"],
        "setup.params.stop_buffer": [0, 0.1, 0.2],
    }

    def launched(self, client: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        entry = an_entry(
            client,
            name=f"zones {uuid.uuid4()}",
            grid=self.GRID,
            document=a_zone_document(f"doc {uuid.uuid4()}"),
        )
        body = a_sweep_body([entry], ["EURUSD"], ["M15"])
        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        )
        created = client.post("/sweeps", json=body)
        assert preview.status_code == 200, preview.text
        assert created.status_code == 202, created.text
        return preview.json(), created.json()

    def test_the_preview_and_the_launch_count_one_run_for_the_three(
        self, client: Any, queue: _CapturingQueue
    ) -> None:
        preview, created = self.launched(client)

        # Three edge points, each reading its buffer; three martelo points, one run.
        assert (preview["documents"], preview["runs"], preview["shared"]) == (6, 4, 2)
        assert (created["runs"], created["shared"]) == (4, 2)
        assert len([job for job in queue.jobs if job[0] == RUN_BACKTEST]) == 4

    def test_the_run_names_the_points_it_answers(self, client: Any) -> None:
        _preview, created = self.launched(client)

        runs = client.get(f"/sweeps/{created['id']}").json()["runs"]

        assert len(runs) == 4
        (martelo,) = [row for row in runs if row["values"]["setup.params.entry_point"] == "martelo"]
        assert sorted(
            one["values"]["setup.params.stop_buffer"] for one in martelo["equivalents"]
        ) == [
            0.1,
            0.2,
        ]
        assert martelo["values"]["setup.params.stop_buffer"] == 0
        assert all(row["equivalents"] == [] for row in runs if row is not martelo)

    def test_the_dataset_keeps_a_row_for_every_point(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        _preview, created = self.launched(client)
        address = f"/sweeps/{created['id']}"
        runs = client.get(address).json()["runs"]
        (martelo,) = [row for row in runs if row["values"]["setup.params.entry_point"] == "martelo"]
        finish(session_factory, martelo["run"]["id"], 250)

        rows = list(csv.DictReader(io.StringIO(client.get(f"{address}/dataset.csv").text)))

        assert len(rows) == 6
        followers = [row for row in rows if row["same_as"]]
        assert len(followers) == 2
        assert {row["same_as"] for row in followers} == {martelo["label"]}
        # The follower's run is the one that answers it, and so is every outcome.
        assert {row["run_id"] for row in followers} == {martelo["run"]["id"]}
        assert {row["return"] for row in followers} == {"0.025"}
        assert sorted(row["param:setup.params.stop_buffer"] for row in followers) == ["0.1", "0.2"]


class TestTheGeneratedName:
    def test_a_grid_whose_label_outgrows_the_name_still_runs(self, client: Any) -> None:
        """⚠️ **This is the sweep that produced nothing.**

        A real five-axis entry called `PC DE COMPRA CLASSICO` expanded to 120 documents, and all
        120 were refused: `name: String should have at most 120 characters`. The strategy was
        fine; the caption the sweep writes into `name` was nine characters too long. The entry
        contributed zero runs to a sweep that looked like it had launched.

        Three axes and a long entry name reproduce it here — the label's length is what matters,
        not which parameters it holds.
        """
        long_name = f"PC DE COMPRA CLASSICO {uuid.uuid4()} {uuid.uuid4()}"
        entry = an_entry(
            client,
            name=long_name,
            grid={
                "setup.params.period": [5, 9, 21],
                "setup.params.breakeven_at_r": [None, 2.0],
                "setup.params.side": ["long", "short"],
            },
        )
        body = a_sweep_body([entry], ["EURUSD"], ["M15"])

        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()
        launched = client.post("/sweeps", json=body)

        assert preview["entries"][0]["refusals"] == []
        assert preview["runs"] == 12
        assert launched.status_code == 202, launched.text
        read = client.get(f"/sweeps/{launched.json()['id']}").json()
        names = {row["run"]["strategy_name"] for row in read["runs"]}
        # Twelve distinct names, every one inside the limit: the trim fits, and the digest keeps
        # points whose visible part is identical from colliding on `(name, version)`.
        assert len(names) == 12
        assert max(len(name) for name in names) <= 120
        assert any("…" in name for name in names), "nothing was trimmed, so this proves nothing"


class TestWhatItRefuses:
    def test_a_backwards_window_is_a_typo_in_both_endpoints(self, client: Any) -> None:
        # ⚠️ **The preview and the launch have to give one verdict.** The launch always
        # refused this; the preview did not, and a backwards window overlaps no dataset at all —
        # so it came back as "no candles in this window; move the window or collect them first",
        # blaming the data and sending a person off to run a backfill they do not need.
        entry = an_entry(client, name=f"backwards {uuid.uuid4()}")
        body = a_sweep_body([entry], ["EURUSD"], ["M15"])
        body["date_from"], body["date_to"] = body["date_to"], body["date_from"]
        asked = {k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")}

        previewed = client.post("/sweeps/preview", json=asked)
        launched = client.post("/sweeps", json=body)

        assert previewed.status_code == 422
        assert launched.status_code == 422
        # Not merely the same code: the same reason, in the same words.
        assert (
            previewed.json()["detail"] == launched.json()["detail"] == "date_to precedes date_from"
        )

    def test_an_unknown_entry_names_every_bad_one(self, client: Any) -> None:
        good = an_entry(client, name=f"real {uuid.uuid4()}")
        missing = str(uuid.uuid4())

        refused = client.post("/sweeps", json=a_sweep_body([good, missing], ["EURUSD"], ["M15"]))

        assert refused.status_code == 404
        assert missing in refused.text

    def test_an_unknown_symbol_names_every_bad_one_and_writes_nothing(
        self, client: Any, queue: _CapturingQueue
    ) -> None:
        entry = an_entry(client, name=f"real {uuid.uuid4()}")

        refused = client.post(
            "/sweeps", json=a_sweep_body([entry], ["EURUSD", "NOPE", "ALSONOPE"], ["M15"])
        )

        assert refused.status_code == 422
        # Both named, not just the first: a caller fixing a typo list one round trip at a time
        # is a caller the API is failing. Two assertions, so a failure says which one is missing.
        assert "NOPE" in refused.text
        assert "ALSONOPE" in refused.text
        assert queue.jobs == []

    def test_a_sweep_past_the_old_cap_is_launched_whole(
        self, client: Any, queue: _CapturingQueue
    ) -> None:
        # ⚠️ His decision (18/09): no cap. 300 points over three markets and four charts is 3600
        # runs — refused by the old cap of 3000, and now written and queued, every one of them.
        # The preview agrees: no `error`, and the same count.
        entry = an_entry(
            client,
            name=f"huge {uuid.uuid4()}",
            grid={"setup.params.period": list(range(3, 303))},
        )
        timeframes = ["M15", "H1", "H4", "D1"]
        queue.jobs.clear()

        previewed = client.post(
            "/sweeps/preview",
            json={
                "entry_ids": [entry],
                "symbols": list(SYMBOLS),
                "timeframes": timeframes,
                "date_from": START.isoformat(),
                "date_to": (START + 100 * HOUR).isoformat(),
            },
        ).json()
        launched = client.post("/sweeps", json=a_sweep_body([entry], list(SYMBOLS), timeframes))

        assert previewed["error"] is None
        assert previewed["runs"] == 3600
        assert launched.status_code == 202, launched.text
        assert launched.json()["runs"] == 3600
        assert len(queue.jobs) == 3600

    def test_a_sweep_with_nothing_runnable_is_an_error_not_a_zero(self, client: Any) -> None:
        # An H4 filter swept only at H4: no combination can run. Reported as an error rather
        # than as `0 runs`, because a screen showing zero beside a live button invites pressing.
        entry = an_entry(
            client,
            name=f"impossible {uuid.uuid4()}",
            document=a_filtered_document(f"choch {uuid.uuid4()}"),
        )
        body = a_sweep_body([entry], ["EURUSD"], ["H4"])

        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()
        assert preview["runs"] == 0
        assert preview["error"] is not None

        assert client.post("/sweeps", json=body).status_code == 422


class TestThePointsStayOffTheShelf:
    def test_a_sweeps_own_points_are_not_offered_as_strategies(self, client: Any) -> None:
        # ⚠️ Without this, one sweep buries every authored strategy. A study writes one document
        # per combination; a sweep writes one per combination **per timeframe**, for every entry
        # — the same failure `study_id` was excluded for, an order of magnitude larger.
        before = client.get("/strategies").json()["total"]
        entry = an_entry(
            client, name=f"swept {uuid.uuid4()}", grid={"setup.params.period": [5, 9, 21]}
        )
        after_entry = client.get("/strategies").json()["total"]

        client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["M15", "H1"]))

        # The entry's own strategy counts; its six generated points do not.
        assert after_entry == before + 1
        assert client.get("/strategies").json()["total"] == after_entry
        # And they are findable when a reader asks for them by name.
        assert client.get("/strategies", params={"include_generated": True}).json()["total"] > (
            after_entry
        )


class TestTheDataHasToBeThere:
    """⚠️ The class this suite gained from the first real sweep.

    Twelve runs were launched against this project's own database and nine failed — each with an
    honest message naming the window the data covers, and each after a worker had picked it up.
    The `datasets` index knew before any of them started; it was simply not asked.
    """

    def test_a_market_with_no_candles_in_the_window_is_skipped_and_named(
        self, client: Any, session: Session, queue: _CapturingQueue
    ) -> None:
        """His rule (18/09): the pair with nothing to read is left out, the rest runs, and the
        sweep says which. It used to refuse the whole launch; a map with a hole is allowed now,
        a map whose hole is not written down is still not."""
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        # Move USDJPY's coverage entirely outside the window the body asks for.
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "USDJPY")
        ):
            dataset.date_from = START + dt.timedelta(days=3650)
            dataset.date_to = START + dt.timedelta(days=4015)
        session.commit()
        queue.jobs.clear()

        launched = client.post("/sweeps", json=a_sweep_body([entry], list(SYMBOLS), ["M15"]))

        assert launched.status_code == 202, launched.text
        body = launched.json()
        assert body["runs"] == 2
        assert [(one["symbol"], one["timeframe"]) for one in body["skipped"]] == [("USDJPY", "M15")]
        # The range it does hold, so a later reader can tell "move the window" from "backfill".
        assert body["skipped"][0]["covers"] is not None
        assert len(queue.jobs) == 2
        # ⚠️ **Kept on the sweep, not only said once.** Read back, the header still lists the
        # three markets asked; this is what tells the reader one of them was never measured.
        read = client.get(f"/sweeps/{body['id']}").json()
        assert read["symbols"] == list(SYMBOLS)
        assert read["skipped"] == body["skipped"]
        assert {row["run"]["symbol"] for row in read["runs"]} == {"EURUSD", "GBPUSD"}

    def test_a_market_is_skipped_by_chart_not_as_a_whole(
        self, client: Any, session: Session
    ) -> None:
        """GBPUSD holds M15 and not H4. Dropping the market would lose three runs that read every
        bar; only the pair with nothing to read is left out."""
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(
            select(Dataset)
            .join(Instrument)
            .where(Instrument.symbol == "GBPUSD", Dataset.timeframe == "H4")
        ):
            session.delete(dataset)
        session.commit()

        launched = client.post(
            "/sweeps", json=a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15", "H4"])
        )

        assert launched.status_code == 202, launched.text
        body = launched.json()
        assert [(one["symbol"], one["timeframe"], one["covers"]) for one in body["skipped"]] == [
            ("GBPUSD", "H4", None)
        ]
        read = client.get(f"/sweeps/{body['id']}").json()
        ran = sorted((row["run"]["symbol"], row["run"]["timeframe"]) for row in read["runs"])
        assert ran == [("EURUSD", "H4"), ("EURUSD", "M15"), ("GBPUSD", "M15")]

    def test_a_chart_skipped_on_every_market_writes_no_points(
        self, client: Any, session: Session
    ) -> None:
        """H4 is missing everywhere: its documents would have no run under them, and writing them
        would put coordinates on the map that nothing measured."""
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(select(Dataset).where(Dataset.timeframe == "H4")):
            session.delete(dataset)
        session.commit()

        launched = client.post(
            "/sweeps", json=a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15", "H4"])
        )

        assert launched.status_code == 202, launched.text
        session.expire_all()
        sweep = session.get(Sweep, uuid.UUID(launched.json()["id"]))
        assert sweep is not None
        assert {point["values"]["timeframe"] for point in sweep.points} == {"M15"}
        assert {(one["symbol"], one["timeframe"]) for one in sweep.skipped} == {
            ("EURUSD", "H4"),
            ("GBPUSD", "H4"),
        }

    def test_a_chart_the_dsl_refuses_is_not_named_as_missing_data(
        self, client: Any, session: Session
    ) -> None:
        """⚠️ The two absences `Sweep.skipped` exists to keep apart, meeting on one pair. This entry
        filters by H4, so H4 is refused by the DSL; EURUSD H4 also has no candles. Named as
        skipped, it would offer a download that could never produce a run."""
        entry = an_entry(
            client,
            name=f"filtered {uuid.uuid4()}",
            document=a_filtered_document(f"choch {uuid.uuid4()}"),
        )
        for dataset in session.scalars(select(Dataset).where(Dataset.timeframe == "H4")):
            session.delete(dataset)
        session.commit()
        body = a_sweep_body([entry], ["EURUSD"], ["M15", "H4"])

        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()
        launched = client.post("/sweeps", json=body)

        assert preview["uncovered"] == []
        # Where it belongs instead: a refusal, in the DSL's words.
        (refusal,) = preview["entries"][0]["refusals"]
        assert refusal["values"]["timeframe"] == "H4"
        assert launched.status_code == 202, launched.text
        assert launched.json()["skipped"] == []

    def test_every_pair_skipped_is_refused_as_missing_data_not_as_an_empty_grid(
        self, client: Any, session: Session, queue: _CapturingQueue
    ) -> None:
        """⚠️ With nothing left, the size rule would say "no combination in this sweep can run" —
        true, and blaming the grid for what is missing data."""
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(select(Dataset).where(Dataset.timeframe == "M15")):
            session.delete(dataset)
        session.commit()
        queue.jobs.clear()

        refused = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15"]))

        assert refused.status_code == 422
        assert refused.json()["detail"] == (
            "no candles in this window for: "
            "EURUSD M15 (never collected), GBPUSD M15 (never collected)"
        )
        assert queue.jobs == []
        session.expire_all()
        assert session.scalars(select(Sweep)).all() == []

    def test_a_chart_that_was_never_collected_is_named_too(
        self, client: Any, session: Session
    ) -> None:
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(select(Dataset).where(Dataset.timeframe == "H4")):
            session.delete(dataset)
        session.commit()

        body = a_sweep_body([entry], ["EURUSD"], ["M15", "H4"])
        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()

        (missing,) = preview["uncovered"]
        assert missing["symbol"] == "EURUSD"
        assert missing["timeframe"] == "H4"
        # ⚠️ `None`, not a range. Never collected and collected for other years are different
        # failures with different fixes: a backfill to run, against a window to move.
        assert missing["covers"] is None

    def test_a_window_that_merely_overlaps_is_allowed(self, client: Any, session: Session) -> None:
        # ⚠️ The half a stricter check would break. A run over the part of the window that exists
        # is a real measurement, and refusing it would make every sweep wait for the
        # least-collected symbol on the list.
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "EURUSD")
        ):
            dataset.date_from = START + dt.timedelta(days=1)
        session.commit()

        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["M15"]))

        assert launched.status_code == 202, launched.text

    def test_the_preview_says_so_before_anything_is_launched(
        self, client: Any, session: Session
    ) -> None:
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "GBPUSD")
        ):
            dataset.date_from = START + dt.timedelta(days=3650)
            dataset.date_to = START + dt.timedelta(days=4015)
        session.commit()

        body = a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["M15"])
        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()

        # ⚠️ Not an error any more: the launch skips the pair rather than refusing (18/09), and a
        # preview that still blocked would hold the button hostage to a no the server never says.
        assert preview["error"] is None
        (uncovered,) = preview["uncovered"]
        assert uncovered["symbol"] == "GBPUSD"
        # The range it *does* hold, so the reader can move the window rather than guess at it.
        assert uncovered["covers"] is not None
        # And the launch agrees, which is the whole point of a preview: the same count, and the
        # same pair named.
        assert preview["runs"] == 1
        launched = client.post("/sweeps", json=body).json()
        assert launched["runs"] == preview["runs"]
        assert launched["skipped"] == preview["uncovered"]

    def test_the_preview_and_the_launch_refuse_an_all_skipped_sweep_in_one_sentence(
        self, client: Any, session: Session
    ) -> None:
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "GBPUSD")
        ):
            dataset.date_from = START + dt.timedelta(days=3650)
            dataset.date_to = START + dt.timedelta(days=4015)
        session.commit()

        body = a_sweep_body([entry], ["GBPUSD"], ["M15"])
        preview = client.post(
            "/sweeps/preview",
            json={
                k: body[k] for k in ("entry_ids", "symbols", "timeframes", "date_from", "date_to")
            },
        ).json()
        refused = client.post("/sweeps", json=body)

        assert preview["runs"] == 0
        assert refused.status_code == 422
        # In the words the basket and the single backtest use too (`coverage.describe`).
        held = (
            f"{(START + dt.timedelta(days=3650)).date().isoformat()} to "
            f"{(START + dt.timedelta(days=4015)).date().isoformat()}"
        )
        assert refused.json()["detail"] == (
            f"no candles in this window for: GBPUSD M15 (on disk: {held})"
        )
        assert preview["error"] == refused.json()["detail"]


def broker_lists(session_factory: Callable[[], Session], *symbols: str) -> None:
    """The host agent's snapshot of the broker's symbols, naming only these."""
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol=symbol) for symbol in symbols],
            server="Tradeview-Demo",
            synced_at=dt.datetime(2026, 9, 10, tzinfo=dt.UTC),
        )
        session.commit()


class TestCollectingWhatASweepIsMissing:
    """His rule (18/09), now for the sweep: "a tela de varredura tb tem que fazer essa coleta".

    ⚠️ **What is different from the basket is the multiplicity.** A pair (market, chart) here is
    shared by every point of every entry, so the download is one per pair and the waiting is
    one row per run. Every test below has several runs on a pair for that reason: with one run
    per pair, a collection per run and a collection per pair are the same number.
    """

    def never_collected(self, session: Session, symbol: str, timeframe: str) -> None:
        for dataset in session.scalars(
            select(Dataset)
            .join(Instrument)
            .where(Instrument.symbol == symbol, Dataset.timeframe == timeframe)
        ):
            session.delete(dataset)
        session.commit()

    def launch(
        self, client: Any, symbols: list[str], timeframes: list[str], **over: Any
    ) -> dict[str, Any]:
        entry = an_entry(
            client, name=f"swept {uuid.uuid4()}", grid={"setup.params.period": [5, 9, 21]}
        )
        body = {**a_sweep_body([entry], symbols, timeframes), **over}
        launched = client.post("/sweeps", json=body)
        assert launched.status_code == 202, launched.text
        return dict(launched.json())

    def waits(self, session: Session, sweep_id: str) -> dict[tuple[str, str], set[uuid.UUID]]:
        """Each pair of the sweep, mapped to the collections its runs wait for.

        ⚠️ Asserts on the way that **every** run over a pair waits for the same ones. Linking
        only the first run of a pair would leave the others free to start on a window still
        being downloaded, and a union over the pair would not notice.
        """
        seen: dict[tuple[str, str], list[set[uuid.UUID]]] = {}
        for run in session.scalars(
            select(Backtest).where(Backtest.sweep_id == uuid.UUID(sweep_id))
        ):
            symbol = session.get(Instrument, run.instrument_id)
            assert symbol is not None
            linked = set(
                session.scalars(
                    select(BacktestCollection.collection_id).where(
                        BacktestCollection.backtest_id == run.id
                    )
                )
            )
            seen.setdefault((symbol.symbol, run.timeframe), []).append(linked)
        out: dict[tuple[str, str], set[uuid.UUID]] = {}
        for pair, each in seen.items():
            assert all(one == each[0] for one in each), pair
            out[pair] = each[0]
        return out

    def test_a_pair_is_collected_once_however_many_runs_read_it(
        self, client: Any, session: Session
    ) -> None:
        """⚠️ **The test this PR exists for.** Three points over GBPUSD M15: a loop copied from the
        basket writes three collections of the same window, and this counts one."""
        self.never_collected(session, "GBPUSD", "M15")

        created = self.launch(client, ["EURUSD", "GBPUSD"], ["M15"], collect_missing=True)

        assert created["runs"] == 6
        session.expire_all()
        (collection,) = session.scalars(select(Collection)).all()
        assert (collection.symbol, collection.timeframe) == ("GBPUSD", "M15")
        waits = self.waits(session, created["id"])
        assert waits[("GBPUSD", "M15")] == {collection.id}
        # The covered market waits for nothing: what can run, runs.
        assert waits[("EURUSD", "M15")] == set()
        assert len(session.scalars(select(BacktestCollection)).all()) == 3

    def test_a_failed_download_is_named_once_however_many_runs_read_it(
        self, client: Any, session: Session
    ) -> None:
        """His rule (22/09): the sweep says which download failed. Three points read GBPUSD M15
        through one collection, so the join finds it three times — and the sweep names it once,
        because what failed is one download, not three."""
        self.never_collected(session, "GBPUSD", "M15")
        created = self.launch(client, ["EURUSD", "GBPUSD"], ["M15"], collect_missing=True)
        assert client.get(f"/sweeps/{created['id']}").json()["failed_collections"] == []

        session.expire_all()
        (collection,) = session.scalars(select(Collection)).all()
        collection.status, collection.error = BacktestStatus.FAILED, "the terminal said no"
        session.commit()

        [failed] = client.get(f"/sweeps/{created['id']}").json()["failed_collections"]
        assert (failed["symbol"], failed["timeframe"], failed["error"]) == (
            "GBPUSD",
            "M15",
            "the terminal said no",
        )

    def test_each_chart_of_a_market_waits_for_its_own_download(
        self, client: Any, session: Session
    ) -> None:
        """A grouping by market alone would pass the test above and hold every M15 run behind
        the H4 backfill here — two charts of one market are two pairs."""
        self.never_collected(session, "GBPUSD", "M15")
        self.never_collected(session, "GBPUSD", "H4")

        created = self.launch(client, ["GBPUSD"], ["M15", "H4"], collect_missing=True)

        session.expire_all()
        by_chart = {one.timeframe: one.id for one in session.scalars(select(Collection))}
        assert set(by_chart) == {"M15", "H4"}
        waits = self.waits(session, created["id"])
        assert waits[("GBPUSD", "M15")] == {by_chart["M15"]}
        assert waits[("GBPUSD", "H4")] == {by_chart["H4"]}

    def test_the_downloads_go_on_the_hosts_queue_and_the_runs_keep_their_job_ids(
        self, client: Any, session: Session, queue: _CapturingQueue
    ) -> None:
        self.never_collected(session, "GBPUSD", "M15")
        queue.jobs.clear()

        self.launch(client, ["EURUSD", "GBPUSD"], ["M15"], collect_missing=True)

        collects = [job for job in queue.jobs if job[0] == COLLECT_RANGE]
        runs = [job for job in queue.jobs if job[0] == RUN_BACKTEST]
        assert [options for _, _, options in collects] == [{"_queue_name": COLLECT_QUEUE}]
        assert len(runs) == 6
        # ⚠️ The idempotency claim survives the new jobs beside it.
        assert all(options == {"_job_id": args[0]} for _, args, options in runs)

    def test_a_market_covered_in_part_waits_for_its_gap(
        self, client: Any, session: Session
    ) -> None:
        """⚠️ Without the flag this sweep runs over the half that exists (the overlap test
        above). Told to collect, it waits for the rest instead: a heading that says the whole
        window over a measurement of half of it is what collecting exists to prevent.

        ⚠️ The gap is a month, not a day: `EDGE_SLACK` forgives four days at an edge, and a
        one-day gap plans nothing — which is how the first draft of this test found it."""
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "EURUSD")
        ):
            dataset.date_from = START + dt.timedelta(days=30)
        session.commit()

        created = self.launch(
            client,
            ["EURUSD", "GBPUSD"],
            ["M15"],
            collect_missing=True,
            date_to=(START + dt.timedelta(days=60)).isoformat(),
        )

        session.expire_all()
        (collection,) = session.scalars(select(Collection)).all()
        assert collection.symbol == "EURUSD"
        assert collection.date_from <= START
        waits = self.waits(session, created["id"])
        assert waits[("EURUSD", "M15")] == {collection.id}
        assert waits[("GBPUSD", "M15")] == set()

    def test_a_pair_missing_both_edges_waits_for_both_downloads(
        self, client: Any, session: Session
    ) -> None:
        """Two windows of one pair: every other test here plans one, so a loop that collected
        only the first window of a plan would pass all of them and start these runs on a year
        still missing.

        ⚠️ **Years of disk between the gaps, or the plan joins them.** Each gap reaches the year
        the disk starts or ends in, so data for 2024 alone and a request from mid-2023 to
        mid-2025 plan one window, 2023 to 2025 — which is how the first draft of this test found
        it. EURUSD holds 2021-2023 here; the sweep asks from mid-2020 to mid-2024."""
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "EURUSD")
        ):
            dataset.date_from = dt.datetime(2021, 1, 4, tzinfo=dt.UTC)
            dataset.date_to = dt.datetime(2023, 12, 29, tzinfo=dt.UTC)
        session.commit()

        created = self.launch(
            client,
            ["EURUSD"],
            ["M15"],
            collect_missing=True,
            date_from=dt.datetime(2020, 6, 1, tzinfo=dt.UTC).isoformat(),
            date_to=dt.datetime(2024, 6, 1, tzinfo=dt.UTC).isoformat(),
        )

        session.expire_all()
        collections = session.scalars(select(Collection).order_by(Collection.date_from)).all()
        assert [(one.date_from.year, one.date_to.year) for one in collections] == [
            (2020, 2021),
            (2023, 2024),
        ]
        waits = self.waits(session, created["id"])
        assert waits[("EURUSD", "M15")] == {one.id for one in collections}

    def test_a_pair_with_nothing_to_collect_and_nothing_to_read_still_refuses_the_sweep(
        self, client: Any, session: Session, queue: _CapturingQueue
    ) -> None:
        """⚠️ An empty plan is not "covered". A window wholly in the future has nothing to fetch
        and no candle either, so the pair is skipped even when told to collect — and being the
        only pair here, nothing is left, the sweep is refused and nothing is written."""
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        ahead = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=30)
        queue.jobs.clear()

        refused = client.post(
            "/sweeps",
            json={
                **a_sweep_body([entry], ["EURUSD"], ["M15"]),
                "date_from": ahead.isoformat(),
                "date_to": (ahead + dt.timedelta(days=10)).isoformat(),
                "collect_missing": True,
            },
        )

        assert refused.status_code == 422
        assert "EURUSD M15" in refused.text
        assert queue.jobs == []
        session.expire_all()
        assert session.scalars(select(Sweep)).all() == []
        assert session.scalars(select(Collection)).all() == []

    def test_told_to_collect_a_pair_with_nothing_to_fetch_is_skipped_beside_one_that_waits(
        self, client: Any, session: Session
    ) -> None:
        """The mixed case: GBPUSD M15 was never collected and can be; EURUSD M15 was never
        collected and the broker's history starts after the window, so there is nothing to
        fetch. One waits for its download, the other is left out and named."""
        self.never_collected(session, "GBPUSD", "M15")
        self.never_collected(session, "EURUSD", "M15")
        session.add(
            SymbolHistory(
                symbol="EURUSD",
                timeframe="M15",
                oldest=START + dt.timedelta(days=1000),
                bar_count=1000,
                terminal_maxbars=100_000,
                probed_at=START,
            )
        )
        session.commit()

        created = self.launch(client, ["EURUSD", "GBPUSD"], ["M15"], collect_missing=True)

        assert created["runs"] == 3
        assert [(one["symbol"], one["timeframe"]) for one in created["skipped"]] == [
            ("EURUSD", "M15")
        ]
        session.expire_all()
        (collection,) = session.scalars(select(Collection)).all()
        assert collection.symbol == "GBPUSD"
        waits = self.waits(session, created["id"])
        assert waits == {("GBPUSD", "M15"): {collection.id}}

    def test_a_pair_the_broker_does_not_list_is_skipped_not_collected(
        self, client: Any, session: Session, session_factory: Callable[[], Session]
    ) -> None:
        """The sweep of 18/09 in miniature: two markets never collected, one of them not at this
        broker. One download, for the market it has; the other is skipped and named."""
        self.never_collected(session, "EURUSD", "M15")
        self.never_collected(session, "GBPUSD", "M15")
        broker_lists(session_factory, "EURUSD")

        created = self.launch(client, ["EURUSD", "GBPUSD"], ["M15"], collect_missing=True)

        assert [(one["symbol"], one["timeframe"]) for one in created["skipped"]] == [
            ("GBPUSD", "M15")
        ]
        session.expire_all()
        (collection,) = session.scalars(select(Collection)).all()
        assert collection.symbol == "EURUSD"

    def test_without_the_flag_nothing_is_collected(self, client: Any, session: Session) -> None:
        self.never_collected(session, "GBPUSD", "M15")
        entry = an_entry(client, name=f"real {uuid.uuid4()}")

        refused = client.post("/sweeps", json=a_sweep_body([entry], ["GBPUSD"], ["M15"]))

        assert refused.status_code == 422
        session.expire_all()
        assert session.scalars(select(Collection)).all() == []


def launch(client: Any, entries: list[str], symbols: list[str]) -> str:
    launched = client.post("/sweeps", json=a_sweep_body(entries, symbols, ["M15"]))
    assert launched.status_code == 202, launched.text
    return str(launched.json()["id"])


def run_ids(client: Any, sweep_id: str) -> list[str]:
    return [row["run"]["id"] for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]]


def move(session_factory: Callable[[], Session], run_id: str, to: BacktestStatus) -> None:
    """Put a queued run in flight, or fail it — the two statuses `finish` does not reach."""
    session = session_factory()
    try:
        run = session.get(Backtest, uuid.UUID(run_id))
        assert run is not None
        run.status = to
        run.started_at = START
        if to is BacktestStatus.FAILED:
            run.error = "the worker lost its database"  # the table refuses a silent failure
        session.commit()
    finally:
        session.close()


def launched_at(session_factory: Callable[[], Session], moments: dict[str, dt.datetime]) -> None:
    session = session_factory()
    try:
        for sweep_id, moment in moments.items():
            sweep = session.get(Sweep, uuid.UUID(sweep_id))
            assert sweep is not None
            sweep.created_at = moment
        session.commit()
    finally:
        session.close()


class TestTheHistory:
    def test_an_empty_history_is_an_empty_page(self, client: Any) -> None:
        listed = client.get("/sweeps")

        assert listed.status_code == 200
        assert listed.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_a_line_says_what_was_asked_and_how_far_it_got(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # ⚠️ **Four statuses, four different counts** (3 done, 2 running, 1 failed, 0 queued), so
        # a count read from the wrong status — or a total taken from one status alone — cannot
        # land on the right number by coincidence. The first draft had three of them at 1, and
        # swapping `done` with `failed` passed it. The second sweep stays all queued, so a count
        # that ignored the sweep id would move it too.
        tag = str(uuid.uuid4())[:8]
        plain = an_entry(client, name=f"alpha {tag}")
        doomed = an_entry(client, name=f"zeta {tag}")
        busy = launch(client, [doomed, plain], list(SYMBOLS))
        idle = launch(client, [plain], ["EURUSD", "GBPUSD"])

        runs = run_ids(client, busy)
        assert len(runs) == 6
        for run_id in runs[:3]:
            finish(session_factory, run_id, 100)
        for run_id in runs[3:5]:
            move(session_factory, run_id, BacktestStatus.RUNNING)
        move(session_factory, runs[5], BacktestStatus.FAILED)
        # Removing an entry costs the line its name and nothing else, as on the sweep's page.
        # Its own statement, not inside the `assert`: `python -O` strips asserts.
        removed = client.delete(f"/catalog/{doomed}")
        assert removed.status_code == 204

        items = {item["id"]: item for item in client.get("/sweeps").json()["items"]}

        line = items[busy]
        assert line["runs"] == {"total": 6, "done": 3, "running": 2, "queued": 0, "failed": 1}
        # In the order they were asked for, which is neither alphabetical nor the shelf's order.
        assert line["entries"] == [
            {"entry_id": doomed, "name": None},
            {"entry_id": plain, "name": f"alpha {tag}"},
        ]
        assert line["symbols"] == list(SYMBOLS)
        assert line["timeframes"] == ["M15"]
        assert dt.datetime.fromisoformat(line["date_from"]) == START
        assert dt.datetime.fromisoformat(line["date_to"]) == START + 100 * HOUR
        assert items[idle]["runs"] == {
            "total": 2,
            "done": 0,
            "running": 0,
            "queued": 2,
            "failed": 0,
        }

    def test_the_newest_sweep_comes_first_and_pages_go_back_in_time(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # ⚠️ **The launch order is chosen after the ids are known**, so that it differs from the
        # four orders a wrong query could fall into: insertion order, its reverse, and the ids
        # either way round. With three random ids any one of those matches a fixed expectation
        # one time in six, and a test that passes a broken query by luck proves nothing.
        entry = an_entry(client, name=f"paged {uuid.uuid4()}")
        inserted = [launch(client, [entry], ["EURUSD"]) for _ in range(3)]
        wrong = {
            tuple(inserted),
            tuple(reversed(inserted)),
            tuple(sorted(inserted, key=uuid.UUID)),
            tuple(sorted(inserted, key=uuid.UUID, reverse=True)),
        }
        newest_first = next(order for order in permutations(inserted) if order not in wrong)
        launched_at(
            session_factory,
            {one: START - dt.timedelta(days=i) for i, one in enumerate(newest_first)},
        )

        whole = client.get("/sweeps").json()
        head = client.get("/sweeps", params={"limit": 2}).json()
        tail = client.get("/sweeps", params={"limit": 2, "offset": 2}).json()

        assert [item["id"] for item in whole["items"]] == list(newest_first)
        assert [item["id"] for item in head["items"]] == list(newest_first[:2])
        assert [item["id"] for item in tail["items"]] == list(newest_first[2:])
        # The total is the history's, not the page's — it is what sizes the pager.
        assert (head["total"], head["limit"], tail["offset"]) == (3, 2, 2)

    def test_sweeps_launched_in_the_same_instant_keep_one_order_across_pages(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # A tie in `created_at` is broken by the id, ascending — without it Postgres may return
        # the two in either order on each read, and a page boundary between them would show one
        # sweep twice and the other never.
        entry = an_entry(client, name=f"tied {uuid.uuid4()}")
        tied = [launch(client, [entry], ["EURUSD"]) for _ in range(2)]
        launched_at(session_factory, dict.fromkeys(tied, START))

        pages = [
            client.get("/sweeps", params={"limit": 1, "offset": offset}).json()["items"]
            for offset in (0, 1)
        ]

        assert [item["id"] for page in pages for item in page] == sorted(tied, key=uuid.UUID)

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}])
    def test_a_page_outside_the_bounds_is_refused(
        self, client: Any, params: dict[str, int]
    ) -> None:
        assert client.get("/sweeps", params=params).status_code == 422

    def test_listing_sweeps_costs_the_same_however_many_there_are(
        self, client: Any, migrated_engine: Engine
    ) -> None:
        """One sweep and three are listed in the same number of queries, and none reads `points`.

        The count holds the shape of the read: a per-sweep query — for its runs, its entries, or
        a lazy load of `points` — makes three sweeps cost more than one. The statement text holds
        what the count cannot see: an undeferred `points` rides along in a query that already
        runs, at no extra count, and it is one element per grid point of every sweep on the page.
        """
        entry = an_entry(
            client, name=f"counted {uuid.uuid4()}", grid={"setup.params.period": [5, 9]}
        )
        launch(client, [entry], ["EURUSD"])

        counted: list[int] = []
        statements: list[str] = []

        def count(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
            counted[-1] += 1
            statements.append(statement)

        def listed() -> int:
            counted.append(0)
            event.listen(migrated_engine, "before_cursor_execute", count)
            try:
                read = client.get("/sweeps")
            finally:
                event.remove(migrated_engine, "before_cursor_execute", count)
            assert read.status_code == 200
            return len(read.json()["items"])

        assert listed() == 1
        launch(client, [entry], ["GBPUSD"])
        launch(client, [entry], ["USDJPY"])
        assert listed() == 3

        one, three = counted
        assert one > 0, "the listener saw nothing, so this proves nothing"
        assert one == three, f"one sweep took {one} queries and three took {three}"
        reading_sweeps = [text for text in statements if "FROM sweeps" in text]
        assert reading_sweeps, "no statement read the sweeps table, so the check below is vacuous"
        assert not any("sweeps.points" in text for text in reading_sweeps)


class TestTheDashboard:
    def test_the_route_is_not_read_as_a_sweep_id_and_starts_empty(self, client: Any) -> None:
        # Declared after `/sweeps/{sweep_id}`, "dashboard" would be a malformed UUID and a 422.
        read = client.get("/sweeps/dashboard")

        assert read.status_code == 200, read.text
        body = read.json()
        assert body["totals"]["sweeps"] == 0
        assert body["totals"]["runs"]["total"] == 0
        assert body["overall"]["median_return"] is None
        assert body["by_entry"] == []
        assert body["sweeps"] == []

    def test_the_window_is_half_open_on_the_launch_instant(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry = an_entry(client, name=f"windowed {uuid.uuid4()}")
        first, second, third = (launch(client, [entry], ["EURUSD"]) for _ in range(3))
        launched_at(
            session_factory,
            {first: START, second: START + HOUR, third: START + 2 * HOUR},
        )

        def listed(**window: dt.datetime) -> list[str]:
            params = {name: moment.isoformat() for name, moment in window.items()}
            read = client.get("/sweeps/dashboard", params=params)
            assert read.status_code == 200, read.text
            return [one["id"] for one in read.json()["sweeps"]]

        # The first instant is in, the last is out — so two adjacent windows share nothing.
        assert listed(launched_from=START + HOUR, launched_to=START + 2 * HOUR) == [second]
        assert listed(launched_from=START + HOUR) == [second, third]
        assert listed(launched_to=START + HOUR) == [first]
        # Oldest first, the order a timeline is read in.
        assert listed() == [first, second, third]
        # The same instant written in another zone is the same instant.
        brasilia = dt.timezone(dt.timedelta(hours=-3))
        assert listed(launched_from=(START + HOUR).astimezone(brasilia)) == [second, third]

    @pytest.mark.parametrize(
        "params",
        [
            {"launched_from": "2026-09-15T00:00:00"},
            {"launched_to": "2026-09-15"},
            {"launched_from": "2026-09-16T00:00:00Z", "launched_to": "2026-09-15T00:00:00Z"},
            {"launched_from": "2026-09-16T00:00:00Z", "launched_to": "2026-09-16T00:00:00Z"},
        ],
    )
    def test_a_window_without_a_zone_or_running_backwards_is_refused(
        self, client: Any, params: dict[str, str]
    ) -> None:
        # A bare date is "the 15th" in nobody's clock; an empty window is a typo, not a question.
        assert client.get("/sweeps/dashboard", params=params).status_code == 422

    def test_runs_are_summarised_by_the_entry_that_launched_them(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # zeta: 2 points x 2 markets = 4 runs, nets 100, 200, 300, -100 → returns 0.01, 0.02,
        #       0.03, -0.01 → median 0.015, 3 winners.
        # alpha: 1 point x 2 markets, nets -100, -300 → median -0.02; then removed from the shelf.
        # A second sweep of zeta alone reuses its documents: its runs must still count as zeta's.
        tag = str(uuid.uuid4())[:8]
        plain = an_entry(client, name=f"alpha {tag}")
        swept = an_entry(client, name=f"zeta {tag}", grid={"setup.params.period": [5, 9]})
        both = launch(client, [swept, plain], ["EURUSD", "GBPUSD"])
        again = launch(client, [swept], ["USDJPY"])

        profits = {swept: iter([100, 200, 300, -100]), plain: iter([-100, -300])}
        for row in client.get(f"/sweeps/{both}").json()["runs"]:
            finish(session_factory, row["run"]["id"], next(profits[row["entry_id"]]))
        removed = client.delete(f"/catalog/{plain}")
        assert removed.status_code == 204

        body = client.get("/sweeps/dashboard").json()

        entries = {one["key"]: one for one in body["by_entry"]}
        assert set(entries) == {swept, plain}
        # 4 finished runs of the first sweep plus 2 queued of the second, all zeta's.
        assert (entries[swept]["runs"], entries[swept]["finished"]) == (6, 4)
        assert entries[swept]["winners"] == 3
        assert Decimal(entries[swept]["median_return"]) == Decimal("0.015")
        assert entries[plain]["label"] is None
        assert Decimal(entries[plain]["median_return"]) == Decimal("-0.02")
        # Ranked by the median: zeta above alpha.
        assert [one["key"] for one in body["by_entry"]] == [swept, plain]

        assert body["totals"]["sweeps"] == 2
        assert body["totals"]["entries"] == 2
        assert body["totals"]["symbols"] == ["EURUSD", "GBPUSD", "USDJPY"]
        assert body["totals"]["runs"]["total"] == 8
        # USDJPY is a market the first sweep never ran, so nothing here is a copy.
        assert body["totals"]["measurements"] == 8
        assert body["overall"]["finished"] == 6
        assert (body["overall"]["winners"], body["overall"]["losers"]) == (3, 3)
        timeline = {one["id"]: one for one in body["sweeps"]}
        assert timeline[both]["entry_names"] == [f"zeta {tag}", None]
        assert (timeline[again]["runs"], timeline[again]["median_return"]) == (2, None)

    def test_a_measurement_repeated_by_another_sweep_counts_once(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        # First sweep, EURUSD + GBPUSD x period 5, 9: nets E5=100, E9=300, G5=-100, G9=-300.
        # Second sweep, EURUSD again, same window and capital: copies, finishing as the engine
        # would — identically. Counted once, the median of (1%, 3%, -1%, -3%) is 0; counted
        # twice, (1, 1, 3, 3, -1, -3) has median 1% and four winners instead of two.
        # Two more sweeps look like copies and are not: a different capital, a different window.
        entry = an_entry(
            client, name=f"repeated {uuid.uuid4()}", grid={"setup.params.period": [5, 9]}
        )
        first = launch(client, [entry], ["EURUSD", "GBPUSD"])
        again = launch(client, [entry], ["EURUSD"])
        richer = client.post(
            "/sweeps",
            json={**a_sweep_body([entry], ["EURUSD"], ["M15"]), "initial_capital": "20000"},
        )
        longer = client.post(
            "/sweeps",
            json={
                **a_sweep_body([entry], ["EURUSD"], ["M15"]),
                "date_to": (START + 200 * HOUR).isoformat(),
            },
        )
        assert (richer.status_code, longer.status_code) == (202, 202)

        nets = {("EURUSD", 5): 100, ("EURUSD", 9): 300, ("GBPUSD", 5): -100, ("GBPUSD", 9): -300}
        for sweep_id in (first, again):
            for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]:
                point = (row["run"]["symbol"], row["values"]["setup.params.period"])
                finish(session_factory, row["run"]["id"], nets[point])

        body = client.get("/sweeps/dashboard").json()

        assert body["totals"]["runs"]["total"] == 10
        assert body["totals"]["measurements"] == 8
        overall = body["overall"]
        assert (overall["finished"], overall["winners"], overall["losers"]) == (4, 2, 2)
        assert Decimal(overall["median_return"]) == 0
        symbols = {one["key"]: one for one in body["by_symbol"]}
        # EURUSD: 2 finished measurements + 4 unfinished (2 richer, 2 longer) = 6, not 8.
        assert (symbols["EURUSD"]["runs"], symbols["EURUSD"]["finished"]) == (6, 2)
        # `finish` records no trades: four measurements that never traded, not six.
        assert body["totals"]["runs_without_trades"] == 4
        # The timeline is per sweep and keeps every run it launched.
        timeline = {one["id"]: one for one in body["sweeps"]}
        assert (timeline[again]["runs"], timeline[again]["finished"]) == (2, 2)
        assert Decimal(timeline[again]["median_return"]) == Decimal("0.02")

    def test_the_dashboard_names_the_pairs_its_sweeps_left_out(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """His call (22/09): read from each sweep's `skipped`, which the launch already records.
        Written by hand here because what is under test is the read, not the launch's rule."""
        entry = an_entry(client, name=f"left out {uuid.uuid4()}")
        first = launch(client, [entry], ["EURUSD"])
        second = launch(client, [entry], ["EURUSD"])
        with session_factory() as session:
            for sweep_id, pairs in (
                (first, [("GBPUSD", "H1"), ("USDJPY", "M15")]),
                (second, [("GBPUSD", "H1")]),
            ):
                row = session.get(Sweep, uuid.UUID(sweep_id))
                assert row is not None
                row.skipped = [
                    {"symbol": symbol, "timeframe": timeframe, "covers": None}
                    for symbol, timeframe in pairs
                ]
            session.commit()

        body = client.get("/sweeps/dashboard").json()

        assert body["totals"]["left_out"] == [
            {"symbol": "GBPUSD", "timeframe": "H1", "sweeps": 2},
            {"symbol": "USDJPY", "timeframe": "M15", "sweeps": 1},
        ]
        timeline = {one["id"]: one["left_out"] for one in body["sweeps"]}
        assert timeline == {first: 2, second: 1}

    def test_the_dashboard_reads_no_equity_curve(
        self, client: Any, session_factory: Callable[[], Session], migrated_engine: Engine
    ) -> None:
        # Deferred, and a query count cannot see it: the column would ride along in the metrics
        # query that already runs. So the statement text is what is asserted.
        entry = an_entry(client, name=f"curveless {uuid.uuid4()}")
        sweep_id = launch(client, [entry], ["EURUSD"])
        for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]:
            finish(session_factory, row["run"]["id"], 100)

        statements: list[str] = []

        def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
            statements.append(statement)

        event.listen(migrated_engine, "before_cursor_execute", record)
        try:
            read = client.get("/sweeps/dashboard")
        finally:
            event.remove(migrated_engine, "before_cursor_execute", record)

        assert read.status_code == 200
        metrics = [text for text in statements if "FROM backtest_metrics" in text]
        assert metrics, "no statement read the metrics, so the check below is vacuous"
        assert not any("equity_curve" in text for text in metrics)

    def test_the_dashboard_costs_the_same_however_many_sweeps_it_reads(
        self, client: Any, migrated_engine: Engine
    ) -> None:
        """A read per sweep would make three sweeps cost more than one.

        ⚠️ Holds below 500 runs: `selectinload` splits its `IN` into batches of 500, so the metrics
        read grows by one query per 500 runs. That growth is bounded and not what this guards.
        """
        entry = an_entry(client, name=f"counted {uuid.uuid4()}")
        launch(client, [entry], ["EURUSD"])

        counted: list[int] = []

        def count(*_args: object) -> None:
            counted[-1] += 1

        def read() -> int:
            counted.append(0)
            event.listen(migrated_engine, "before_cursor_execute", count)
            try:
                body = client.get("/sweeps/dashboard")
            finally:
                event.remove(migrated_engine, "before_cursor_execute", count)
            assert body.status_code == 200
            return int(body.json()["totals"]["sweeps"])

        assert read() == 1
        launch(client, [entry], ["GBPUSD"])
        launch(client, [entry], ["USDJPY"])
        assert read() == 3

        one, three = counted
        assert one > 0, "the listener saw nothing, so this proves nothing"
        assert one == three, f"one sweep took {one} queries and three took {three}"


def finish_trading(
    session_factory: Callable[[], Session], run_id: str, net: int, trades: int
) -> None:
    """`finish`, with trades: a run the holdout can rank has to have traded past the floor."""
    finish(session_factory, run_id, net)
    with session_factory() as session:
        run = session.get(Backtest, uuid.UUID(run_id))
        assert run is not None
        assert run.metrics is not None
        run.metrics.total_trades = trades
        run.metrics.long_trades = trades
        session.commit()


class TestTheReservedWindow:
    """His ask (24/09): run a sweep's best points again on a window none of them was chosen on."""

    def swept(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> tuple[str, list[dict[str, Any]]]:
        """A finished sweep of five points over EURUSD H1, ranked 100..500 by net."""
        entry = an_entry(
            client, name=f"held {uuid.uuid4()}", grid={"setup.params.period": [5, 7, 9, 11, 13]}
        )
        launched = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"]))
        assert launched.status_code == 202, launched.text
        sweep_id = launched.json()["id"]
        runs = client.get(f"/sweeps/{sweep_id}").json()["runs"]
        for row in runs:
            period = row["values"]["setup.params.period"]
            finish_trading(session_factory, row["run"]["id"], net=period * 100, trades=40)
        return sweep_id, runs

    def after(self, **over: Any) -> dict[str, Any]:
        return {
            "date_from": (START + 200 * HOUR).isoformat(),
            "date_to": (START + 300 * HOUR).isoformat(),
            "top_n": 2,
            **over,
        }

    def test_the_best_points_run_again_on_the_reserved_window(
        self, client: Any, session_factory: Callable[[], Session], queue: _CapturingQueue
    ) -> None:
        sweep_id, runs = self.swept(client, session_factory)
        queued_before = len(queue.jobs)

        created = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after())

        assert created.status_code == 202, created.text
        assert created.json()["runs"] == 2
        assert len(queue.jobs) - queued_before == 2
        holdout = client.get(f"/sweeps/{created.json()['id']}").json()
        assert holdout["holdout_of"] == sweep_id
        assert holdout["holdout_rule"] == {
            "metric": "net_profit",
            "top_n": 2,
            "min_trades": {"H1": 30},
        }
        # The two best — periods 13 and 11 — on the same documents, over the new window.
        by_strategy = {row["run"]["strategy_id"]: row for row in runs}
        tested = holdout["runs"]
        assert sorted(row["values"]["setup.params.period"] for row in tested) == [11, 13]
        assert {row["run"]["strategy_id"] for row in tested} <= set(by_strategy)
        assert {dt.datetime.fromisoformat(row["run"]["date_from"]) for row in tested} == {
            START + 200 * HOUR
        }

    def test_the_comparison_sets_each_point_beside_the_run_it_was_chosen_by(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.swept(client, session_factory)
        holdout_id = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after()).json()["id"]
        for row in client.get(f"/sweeps/{holdout_id}").json()["runs"]:
            # Period 13 held up, period 11 did not.
            net = 50 if row["values"]["setup.params.period"] == 13 else -80
            finish_trading(session_factory, row["run"]["id"], net=net, trades=10)

        compared = client.get(f"/sweeps/{holdout_id}/holdout")

        assert compared.status_code == 200, compared.text
        body = compared.json()
        assert body["holdout_of"] == sweep_id
        by_period = {row["values"]["setup.params.period"]: row for row in body["rows"]}
        assert Decimal(by_period[13]["in_sample"]["net_return"]) == Decimal("0.13")
        assert Decimal(by_period[13]["out_of_sample"]["net_return"]) == Decimal("0.005")
        assert Decimal(by_period[11]["out_of_sample"]["net_return"]) == Decimal("-0.008")
        (group,) = body["groups"]
        assert (group["points"], group["done"]) == (2, 2)
        assert Decimal(group["in_sample_median_return"]) == Decimal("0.12")
        assert Decimal(group["out_of_sample_positive"]) == Decimal("0.5")

    def test_a_window_that_shares_a_bar_with_the_search_is_refused(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.swept(client, session_factory)

        inside = client.post(
            f"/sweeps/{sweep_id}/holdout",
            json=self.after(date_from=(START + 50 * HOUR).isoformat()),
        )

        assert inside.status_code == 422
        assert "out of sample" in inside.json()["detail"]

    def test_a_test_is_not_tested_again(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.swept(client, session_factory)
        holdout_id = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after()).json()["id"]

        again = client.post(
            f"/sweeps/{holdout_id}/holdout",
            json=self.after(
                date_from=(START + 400 * HOUR).isoformat(), date_to=(START + 500 * HOUR).isoformat()
            ),
        )

        assert again.status_code == 422
        assert "reserved-window test" in again.json()["detail"]

    def test_a_sweep_with_nothing_past_the_trade_floor_is_refused(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry = an_entry(client, name=f"thin {uuid.uuid4()}")
        sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()[
            "id"
        ]
        (row,) = client.get(f"/sweeps/{sweep_id}").json()["runs"]
        finish_trading(session_factory, row["run"]["id"], net=900, trades=29)

        refused = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after())

        assert refused.status_code == 422
        assert "trade floor" in refused.json()["detail"]

    def test_an_ordinary_sweep_has_no_comparison(self, client: Any) -> None:
        entry = an_entry(client, name=f"plain {uuid.uuid4()}")
        sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()[
            "id"
        ]

        assert client.get(f"/sweeps/{sweep_id}/holdout").status_code == 404
        assert client.post(f"/sweeps/{uuid.uuid4()}/holdout", json=self.after()).status_code == 404

    def test_the_trade_floor_can_be_raised_per_chart(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """His ask (24/09): the sweep's floor is 1 trade on D1 and W1, which ranked runs of one to
        four trades there. Raised here to 41 on H1, every run of 40 falls below it."""
        sweep_id, _runs = self.swept(client, session_factory)

        raised = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after(min_trades={"H1": 41}))
        kept = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after(min_trades={"H1": 40}))

        assert raised.status_code == 422
        assert kept.status_code == 202, kept.text
        rule = client.get(f"/sweeps/{kept.json()['id']}").json()["holdout_rule"]
        assert rule["min_trades"] == {"H1": 40}

    def test_a_floor_below_one_or_on_an_unknown_chart_is_refused(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.swept(client, session_factory)

        for floors in ({"H1": 0}, {"H7": 5}):
            refused = client.post(f"/sweeps/{sweep_id}/holdout", json=self.after(min_trades=floors))
            assert refused.status_code == 422, floors


def trade_in(
    session_factory: Callable[[], Session], run_id: str, rs: list[str], *, first: dt.datetime
) -> None:
    """Closed trades on a run, one a day from `first`, each making the R given."""
    with session_factory() as session:
        run = session.get(Backtest, uuid.UUID(run_id))
        assert run is not None
        run.recorded = Recorded.TRADES
        for day, r in enumerate(rs):
            entered = first + dt.timedelta(days=day)
            session.add(
                Trade(
                    backtest_id=run.id,
                    instrument_id=run.instrument_id,
                    direction=Side.LONG,
                    entry_time=entered,
                    entry_price=Decimal("1.10000"),
                    volume=Decimal("0.10"),
                    stop_loss=Decimal("1.09000"),
                    exit_time=entered + HOUR,
                    exit_price=Decimal("1.10500"),
                    exit_reason=ExitReason.TAKE_PROFIT,
                    gross_pnl=Decimal(r) * 100,
                    costs=Decimal("0"),
                    net_pnl=Decimal(r) * 100,
                    r_multiple=Decimal(r),
                )
            )
        session.commit()


class TestTheSummaryIsKept:
    """25/09: once every run has ended, the per-entry summary is computed once and kept. Measured
    on 51 840 runs: 6.5 s a poll computed."""

    def settled(self, client: Any, session_factory: Callable[[], Session]) -> tuple[str, list[str]]:
        entry = an_entry(
            client, name=f"kept {uuid.uuid4()}", grid={"setup.params.period": [5, 7, 9]}
        )
        sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()[
            "id"
        ]
        runs = [row["run"]["id"] for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]]
        for run_id, net in zip(runs, (100, 300, 200), strict=True):
            finish(session_factory, run_id, net)
        return sweep_id, runs

    def kept(self, session_factory: Callable[[], Session], sweep_id: str) -> Any:
        with session_factory() as session:
            sweep = session.get(Sweep, uuid.UUID(sweep_id))
            assert sweep is not None
            return sweep.summary

    def test_nothing_is_kept_while_a_run_is_still_to_come(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry = an_entry(client, name=f"open {uuid.uuid4()}", grid={"setup.params.period": [5, 7]})
        sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()[
            "id"
        ]
        first = client.get(f"/sweeps/{sweep_id}").json()["runs"][0]["run"]["id"]
        finish(session_factory, first, 100)

        body = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()

        assert body["counts"]["queued"] == 1
        assert body["entries"][0]["aggregate"]["points_finished"] == 1
        assert self.kept(session_factory, sweep_id) is None

    def test_a_settled_sweep_keeps_its_summary_and_serves_it(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.settled(client, session_factory)

        first = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()
        kept = self.kept(session_factory, sweep_id)

        assert kept is not None
        assert kept["counts"] == first["counts"]
        assert Decimal(first["entries"][0]["aggregate"]["median_return"]) == Decimal("0.02")
        # ⚠️ Proof that the kept copy is what is served: change it in the database, and the
        # next read says what the database says — nothing was computed again.
        with session_factory() as session:
            sweep = session.get(Sweep, uuid.UUID(sweep_id))
            assert sweep is not None
            summary = dict(sweep.summary or {})
            entries = [dict(one) for one in summary["entries"]]
            entries[0] = {
                **entries[0],
                "aggregate": {**entries[0]["aggregate"], "median_return": "9"},
            }
            sweep.summary = {**summary, "entries": entries}
            session.commit()
        again = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()
        assert again["entries"][0]["aggregate"]["median_return"] == "9"

    def test_a_run_that_changes_makes_it_be_computed_again(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, runs = self.settled(client, session_factory)
        client.get(f"/sweeps/{sweep_id}", params={"runs": "none"})
        # A run that failed on its retry: the counts move, and nothing kept may answer.
        with session_factory() as session:
            run = session.get(Backtest, uuid.UUID(runs[1]))
            assert run is not None
            run.status = BacktestStatus.FAILED
            run.error = "failed on its retry"
            session.commit()

        body = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()

        assert body["counts"]["failed"] == 1
        assert body["entries"][0]["aggregate"]["points_failed"] == 1
        assert self.kept(session_factory, sweep_id)["counts"] == body["counts"]

    def test_the_entry_name_is_read_live_even_from_a_kept_summary(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = self.settled(client, session_factory)
        client.get(f"/sweeps/{sweep_id}", params={"runs": "none"})
        with session_factory() as session:
            sweep = session.get(Sweep, uuid.UUID(sweep_id))
            assert sweep is not None
            entry = session.get(CatalogEntry, uuid.UUID(sweep.entry_ids[0]))
            assert entry is not None
            entry.name = "renamed on the shelf"
            session.commit()

        body = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()

        assert body["entries"][0]["entry_name"] == "renamed on the shelf"


class TestChoosingByRiskInR:
    """25/09: a reserved-window test can rank by the risk in R and set limits on it."""

    def test_the_limits_decide_what_can_be_chosen_and_are_kept_in_the_rule(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        window = TestTheReservedWindow()
        sweep_id, runs = window.swept(client, session_factory)
        by_period = {row["values"]["setup.params.period"]: row["run"]["id"] for row in runs}
        # Period 13 made the most money but fell 30 R deep; 11 and 9 stayed within 10 R.
        with session_factory() as session:
            for period, (net_r, drawdown_r, years) in {
                13: ("40", "30", "0.8"),
                11: ("15", "5", "0.75"),
                9: ("12", "3", "0.5"),
            }.items():
                run = session.get(Backtest, uuid.UUID(by_period[period]))
                assert run is not None
                assert run.metrics is not None
                run.metrics.net_r = Decimal(net_r)
                run.metrics.max_drawdown_r = Decimal(drawdown_r)
                run.metrics.positive_year_share = Decimal(years)
            session.commit()

        created = client.post(
            f"/sweeps/{sweep_id}/holdout",
            json=window.after(
                top_n=1, metric="recovery_r", max_drawdown_r="10", min_positive_year_share="0.6"
            ),
        )

        assert created.status_code == 202, created.text
        test = client.get(f"/sweeps/{created.json()['id']}").json()
        # 13 is too deep and 9 too unsteady; 7 and 5 were recorded without R and never pass.
        assert [row["values"]["setup.params.period"] for row in test["runs"]] == [11]
        assert test["holdout_rule"]["metric"] == "recovery_r"
        assert test["holdout_rule"]["max_drawdown_r"] == "10"
        assert test["holdout_rule"]["min_positive_year_share"] == "0.6"

    def test_limits_nothing_passes_are_refused_with_the_reason(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        window = TestTheReservedWindow()
        sweep_id, _runs = window.swept(client, session_factory)

        refused = client.post(f"/sweeps/{sweep_id}/holdout", json=window.after(max_drawdown_r="5"))

        assert refused.status_code == 422
        assert "before 25/09 has no risk in R" in refused.json()["detail"]


class TestResamplingATest:
    """25/09: a finished reserved-window test's points resampled by hand, and the answer kept."""

    def a_finished_test(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> tuple[str, dict[int, str]]:
        """Period 13 kept 25 trades (+1, +1, -1 repeating, then +1); 11 lost and kept none."""
        window = TestTheReservedWindow()
        sweep_id, _runs = window.swept(client, session_factory)
        test_id = client.post(f"/sweeps/{sweep_id}/holdout", json=window.after()).json()["id"]
        runs = {
            row["values"]["setup.params.period"]: row["run"]["id"]
            for row in client.get(f"/sweeps/{test_id}").json()["runs"]
        }
        finish_trading(session_factory, runs[13], net=900, trades=25)
        trade_in(session_factory, runs[13], ["1", "1", "-1"] * 8 + ["1"], first=START + 210 * HOUR)
        finish_trading(session_factory, runs[11], net=-80, trades=4)
        with session_factory() as session:
            lost = session.get(Backtest, uuid.UUID(runs[11]))
            assert lost is not None
            lost.recorded = Recorded.METRICS
            session.commit()
        return test_id, runs

    def test_each_point_is_resampled_beside_what_happened_and_the_answer_is_kept(
        self, client: Any, session_factory: Callable[[], Session], queue: _CapturingQueue
    ) -> None:
        test_id, runs = self.a_finished_test(client, session_factory)
        queued = len(queue.jobs)

        made = client.post(f"/sweeps/{test_id}/montecarlos", json={"paths": 200, "seed": "x"})

        assert made.status_code == 201, made.text
        assert len(queue.jobs) == queued, "resampling reads stored trades; it queues nothing"
        body = made.json()
        assert (body["paths"], body["seed"]) == (200, "x")
        points = {one["run_id"]: one for one in body["points"]}
        good = points[runs[13]]
        # What happened: +9 R; the one-loss dips never go deeper than 1 R; streaks of one.
        assert (good["trades"], Decimal(good["observed_net_r"])) == (25, Decimal(9))
        assert Decimal(good["observed_drawdown_r"]) == Decimal(1)
        assert good["observed_losing_streak"] == 1
        simulated = good["simulated"]
        assert (simulated["paths"], simulated["trades"]) == (200, 25)
        # Drawn with replacement, some path meets losses back to back that never happened.
        assert Decimal(simulated["losing_streak"]["p99"]) >= 2
        assert Decimal(simulated["drawdown_r"]["p99"]) >= Decimal(simulated["drawdown_r"]["p50"])
        lost = points[runs[11]]
        assert (lost["trades_kept"], lost["simulated"]) == (False, None)

        kept = client.get(f"/sweeps/{test_id}/montecarlos").json()
        assert [one["id"] for one in kept] == [body["id"]]

    def test_the_same_seed_gives_the_same_answer_and_a_blank_one_is_drawn_and_kept(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        test_id, runs = self.a_finished_test(client, session_factory)

        first = client.post(f"/sweeps/{test_id}/montecarlos", json={"paths": 150, "seed": "s"})
        again = client.post(f"/sweeps/{test_id}/montecarlos", json={"paths": 150, "seed": "s"})
        drawn = client.post(f"/sweeps/{test_id}/montecarlos", json={"paths": 150})

        def simulated(response: Any) -> Any:
            return {one["run_id"]: one for one in response.json()["points"]}[runs[13]]["simulated"]

        assert simulated(first) == simulated(again)
        assert drawn.json()["seed"] not in ("", None)

    def test_only_a_finished_reserved_window_test_is_resampled(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        window = TestTheReservedWindow()
        sweep_id, _runs = window.swept(client, session_factory)

        assert client.post(f"/sweeps/{sweep_id}/montecarlos", json={}).status_code == 422
        test_id = client.post(f"/sweeps/{sweep_id}/holdout", json=window.after()).json()["id"]
        assert client.post(f"/sweeps/{test_id}/montecarlos", json={}).status_code == 409
        assert client.post(f"/sweeps/{test_id}/montecarlos", json={"paths": 50}).status_code == 422
        assert client.get(f"/sweeps/{uuid.uuid4()}/montecarlos").status_code == 404


class TestJudgingATestInPieces:
    """His ask (25/09): cut a finished reserved-window test by year or into blocks of trades, by
    hand, and keep the answer."""

    def a_finished_test(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> tuple[str, dict[int, str]]:
        """A finished test of periods 13 and 11: 13 kept six trades, 11 lost and kept none."""
        window = TestTheReservedWindow()
        sweep_id, _runs = window.swept(client, session_factory)
        test_id = client.post(f"/sweeps/{sweep_id}/holdout", json=window.after()).json()["id"]
        runs = {
            row["values"]["setup.params.period"]: row["run"]["id"]
            for row in client.get(f"/sweeps/{test_id}").json()["runs"]
        }
        finish_trading(session_factory, runs[13], net=300, trades=6)
        # Blocks of three: [1, 1, -1] = +1 and [1, 1, 1] = +3 → both positive.
        trade_in(
            session_factory, runs[13], ["1", "1", "-1", "1", "1", "1"], first=START + 210 * HOUR
        )
        finish_trading(session_factory, runs[11], net=-80, trades=4)
        with session_factory() as session:
            lost = session.get(Backtest, uuid.UUID(runs[11]))
            assert lost is not None
            lost.recorded = Recorded.METRICS  # a test run from before 25/09 that lost
            session.commit()
        return test_id, runs

    def test_blocks_of_trades_are_judged_and_the_answer_is_kept(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        test_id, runs = self.a_finished_test(client, session_factory)

        made = client.post(
            f"/sweeps/{test_id}/slicings",
            json={"mode": "trades", "block_trades": 4, "pass_share": "0.5"},
        )
        assert made.status_code == 422, "a block of fewer than five is refused"
        made = client.post(
            f"/sweeps/{test_id}/slicings", json={"mode": "calendar", "block_trades": 5}
        )
        assert made.status_code == 422, "a block size by calendar means nothing"

        made = client.post(
            f"/sweeps/{test_id}/slicings",
            json={"mode": "trades", "block_trades": 5, "pass_share": "0.7"},
        )
        assert made.status_code == 201, made.text
        body = made.json()
        points = {one["run_id"]: one for one in body["points"]}
        good = points[runs[13]]
        assert [
            (one["label"], one["trades"], Decimal(one["net_r"]), one["counted"])
            for one in good["slices"]
        ] == [
            ("1-5", 5, Decimal("3"), True),
            ("6-6", 1, Decimal("1"), False),
        ]
        # One counted block is one draw: never a pass, however good.
        assert (good["counted"], good["positive"], good["passed"]) == (1, 1, False)
        assert Decimal(good["net_r"]) == Decimal("4")
        lost = points[runs[11]]
        assert (lost["trades_kept"], lost["slices"], lost["passed"]) == (False, [], False)
        (group,) = body["groups"]
        assert (group["points"], group["judged"], group["passed"]) == (2, 1, 0)

        kept = client.get(f"/sweeps/{test_id}/slicings")
        assert kept.status_code == 200
        assert [one["id"] for one in kept.json()] == [body["id"]]

    def test_a_second_cut_of_the_same_test_runs_nothing_and_is_kept_beside_the_first(
        self, client: Any, session_factory: Callable[[], Session], queue: _CapturingQueue
    ) -> None:
        test_id, runs = self.a_finished_test(client, session_factory)
        first = client.post(
            f"/sweeps/{test_id}/slicings", json={"mode": "calendar", "pass_share": "0.7"}
        ).json()
        queued = len(queue.jobs)

        second = client.post(
            f"/sweeps/{test_id}/slicings",
            json={"mode": "trades", "block_trades": 5, "pass_share": "0.7"},
        )

        assert second.status_code == 201
        assert len(queue.jobs) == queued, "a slicing reads stored trades; it queues nothing"
        good = {one["run_id"]: one for one in first["points"]}[runs[13]]
        assert [one["label"] for one in good["slices"]] == ["2024"]
        listed = client.get(f"/sweeps/{test_id}/slicings").json()
        assert [one["mode"] for one in listed] == ["trades", "calendar"]

    def test_only_a_finished_reserved_window_test_can_be_judged(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        window = TestTheReservedWindow()
        sweep_id, _runs = window.swept(client, session_factory)
        body = {"mode": "calendar"}

        assert client.post(f"/sweeps/{sweep_id}/slicings", json=body).status_code == 422
        test_id = client.post(f"/sweeps/{sweep_id}/holdout", json=window.after()).json()["id"]
        still = client.post(f"/sweeps/{test_id}/slicings", json=body)
        assert still.status_code == 409, still.text
        assert client.post(f"/sweeps/{uuid.uuid4()}/slicings", json=body).status_code == 404
        assert client.get(f"/sweeps/{uuid.uuid4()}/slicings").status_code == 404


class TestTheScreenReadsAPage:
    """A sweep of 22 thousand runs answered with all of them was 89 MB per poll (24/09): the screen
    now polls without the runs and reads them a ranked page at a time."""

    def five(self, client: Any, session_factory: Callable[[], Session]) -> tuple[str, list[str]]:
        """Five points of one entry; four finished with nets 100..400, the fifth still queued."""
        entry = an_entry(
            client, name=f"paged {uuid.uuid4()}", grid={"setup.params.period": [5, 7, 9, 11, 13]}
        )
        sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()[
            "id"
        ]
        runs = client.get(f"/sweeps/{sweep_id}").json()["runs"]
        by_period = {row["values"]["setup.params.period"]: row["run"]["id"] for row in runs}
        for period, net in ((5, 100), (7, 400), (9, 200), (11, 300)):
            finish_trading(session_factory, by_period[period], net=net, trades=10)
        return sweep_id, [by_period[p] for p in (7, 11, 9, 5, 13)]

    def test_polling_carries_the_counts_and_no_runs(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _ = self.five(client, session_factory)

        body = client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()

        assert body["runs"] == []
        assert body["counts"] == {"total": 5, "done": 4, "running": 0, "queued": 1, "failed": 0}
        # The summaries are still the whole sweep's.
        (entry,) = body["entries"]
        assert entry["aggregate"]["points_finished"] == 4
        assert client.get(f"/sweeps/{sweep_id}").json()["counts"]["total"] == 5

    def test_a_page_is_ranked_best_first_with_the_unfinished_last(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, best_first = self.five(client, session_factory)

        first = client.get(f"/sweeps/{sweep_id}/runs", params={"limit": 2}).json()
        rest = client.get(f"/sweeps/{sweep_id}/runs", params={"limit": 2, "offset": 2}).json()
        last = client.get(f"/sweeps/{sweep_id}/runs", params={"limit": 2, "offset": 4}).json()

        assert first["total"] == 5
        ids = [row["run"]["id"] for page in (first, rest, last) for row in page["items"]]
        assert ids == best_first

    def test_the_smallest_drawdown_ranks_first_and_a_profit_factor_with_no_loss_leads(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, best_first = self.five(client, session_factory)
        with session_factory() as session:
            for run_id, (drawdown, factor) in zip(
                best_first[:4],
                [("0.30", "1.5"), ("0.10", None), ("0.20", "2.5"), ("0.05", "0.8")],
                strict=True,
            ):
                run = session.get(Backtest, uuid.UUID(run_id))
                assert run is not None
                assert run.metrics is not None
                run.metrics.max_drawdown_pct = Decimal(drawdown)
                run.metrics.profit_factor = None if factor is None else Decimal(factor)
            session.commit()

        def ranked(by: str) -> list[str]:
            page = client.get(f"/sweeps/{sweep_id}/runs", params={"rank_by": by}).json()
            return [row["run"]["id"] for row in page["items"]]

        n7, n11, n9, n5, queued = best_first
        assert ranked("drawdown") == [n5, n11, n9, n7, queued]
        # Period 11 won with no loss (its net is positive): the best profit factor there is.
        assert ranked("profit_factor") == [n11, n9, n7, n5, queued]

    def test_the_measures_in_r_rank_with_their_edge_cases_and_old_runs_last(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        """25/09. Four finished runs with their risk in R, and the queued one with none — which
        stands for every run recorded before these measures existed."""
        sweep_id, best_first = self.five(client, session_factory)
        n7, n11, n9, n5, queued = best_first
        # (net R, drawdown R, positive-year share)
        risk = {
            n7: ("20", "25", "0.4"),  # recovery 0.8
            n11: ("12", "4", "0.8"),  # recovery 3
            n9: ("3", "0", None),  # gained with no drawdown: best recovery; one year, no share
            n5: ("-2", "6", "0.5"),  # recovery below zero
        }
        with session_factory() as session:
            for run_id, (net_r, drawdown_r, years) in risk.items():
                run = session.get(Backtest, uuid.UUID(run_id))
                assert run is not None
                assert run.metrics is not None
                run.metrics.net_r = Decimal(net_r)
                run.metrics.max_drawdown_r = Decimal(drawdown_r)
                run.metrics.positive_year_share = None if years is None else Decimal(years)
            session.commit()

        def ranked(by: str) -> list[str]:
            page = client.get(f"/sweeps/{sweep_id}/runs", params={"rank_by": by}).json()
            return [row["run"]["id"] for row in page["items"]]

        assert ranked("net_r") == [n7, n11, n9, n5, queued]
        assert ranked("recovery_r") == [n9, n11, n7, n5, queued]
        assert ranked("drawdown_r") == [n9, n11, n5, n7, queued]
        # n9 has no share (one year) and ranks with the run that has no R at all: last. The two tie,
        # and a tie falls to the strategy's name (runs launched together share `created_at`).
        by_years = ranked("positive_years")
        assert by_years[:3] == [n11, n5, n7]
        assert set(by_years[3:]) == {n9, queued}

    def test_a_page_of_one_entry_holds_only_its_runs(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        mine = an_entry(client, name=f"mine {uuid.uuid4()}", grid={"setup.params.period": [5, 7]})
        other = an_entry(client, name=f"other {uuid.uuid4()}")
        sweep_id = client.post(
            "/sweeps", json=a_sweep_body([mine, other], ["EURUSD"], ["H1"])
        ).json()["id"]

        page = client.get(f"/sweeps/{sweep_id}/runs", params={"entry_id": mine}).json()

        assert page["total"] == 2
        assert {row["entry_id"] for row in page["items"]} == {mine}
        assert client.get(f"/sweeps/{uuid.uuid4()}/runs").status_code == 404


class TestEachMarketPaysItsOwnCosts:
    """His account pays a spread and a commission per lot (24/09), and a sweep over several
    markets used to charge one spread to them all."""

    def test_each_run_is_charged_its_markets_spread_and_the_commission(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        with session_factory() as session:
            gbp = session.scalars(select(Instrument).where(Instrument.symbol == "GBPUSD")).one()
            gbp.default_spread_points = Decimal(12)
            session.commit()
        entry = an_entry(client, name=f"costed {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["H1"]),
            "cost_model": {"type": "instrument", "commission_per_unit": "3.5"},
        }

        launched = client.post("/sweeps", json=body)

        assert launched.status_code == 202, launched.text
        runs = client.get(f"/sweeps/{launched.json()['id']}").json()["runs"]
        charged = {row["run"]["symbol"]: row["run"]["cost_model"] for row in runs}
        assert charged == {
            "EURUSD": {
                "type": "spread_commission",
                "spread_points": "8.0000000000",
                "commission_per_unit": "3.5",
            },
            "GBPUSD": {
                "type": "spread_commission",
                "spread_points": "12.0000000000",
                "commission_per_unit": "3.5",
            },
        }

    def test_a_market_nobody_measured_is_refused_by_name(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        with session_factory() as session:
            gbp = session.scalars(select(Instrument).where(Instrument.symbol == "GBPUSD")).one()
            gbp.default_spread_points = None
            session.commit()
        entry = an_entry(client, name=f"unmeasured {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["H1"]),
            "cost_model": {"type": "instrument"},
        }

        refused = client.post("/sweeps", json=body)

        assert refused.status_code == 422
        assert "no measured spread for: GBPUSD" in refused.json()["detail"]

    @pytest.mark.parametrize(
        "model",
        [
            {"type": "teleport"},
            {"type": "spread", "spread_points": "-1"},
            {"type": "instrument", "commission_per_unit": "lots"},
        ],
    )
    def test_a_cost_the_engine_cannot_charge_is_refused_before_any_run(
        self, client: Any, model: dict[str, Any]
    ) -> None:
        entry = an_entry(client, name=f"bad cost {uuid.uuid4()}")
        body = {**a_sweep_body([entry], ["EURUSD"], ["H1"]), "cost_model": model}

        refused = client.post("/sweeps", json=body)

        assert refused.status_code == 422, refused.text
        assert "cost model cannot be charged" in refused.json()["detail"]

    def test_costs_typed_at_launch_are_charged_market_by_market(self, client: Any) -> None:
        """His ask (24/09): the costs are the broker's and change with it, so they are typed with
        the sweep — GBPUSD's 5 points on his account, with no commission."""
        entry = an_entry(client, name=f"typed {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["H1"]),
            "cost_model": {
                "type": "per_market",
                "markets": {
                    "EURUSD": {"spread_points": "8", "commission_per_unit": "3.5"},
                    "GBPUSD": {"spread_points": "5"},
                },
            },
        }

        launched = client.post("/sweeps", json=body)

        assert launched.status_code == 202, launched.text
        runs = client.get(f"/sweeps/{launched.json()['id']}").json()["runs"]
        charged = {row["run"]["symbol"]: row["run"]["cost_model"] for row in runs}
        assert charged["GBPUSD"] == {
            "type": "spread_commission",
            "spread_points": "5",
            "commission_per_unit": "0",
        }
        assert charged["EURUSD"]["commission_per_unit"] == "3.5"

    def test_a_market_left_out_of_the_typed_costs_is_refused_by_name(self, client: Any) -> None:
        entry = an_entry(client, name=f"half typed {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["EURUSD", "GBPUSD"], ["H1"]),
            "cost_model": {"type": "per_market", "markets": {"EURUSD": {"spread_points": "8"}}},
        }

        refused = client.post("/sweeps", json=body)

        assert refused.status_code == 422
        assert refused.json()["detail"] == "no costs given for: GBPUSD"

    def test_a_swap_typed_per_side_is_written_on_every_run_signed(self, client: Any) -> None:
        """His GBPUSD (24/09): 5 points of spread, and -5 USD per lot per night on both sides —
        signed as the broker quotes it, since a swap can be a credit."""
        entry = an_entry(client, name=f"swapped {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["GBPUSD"], ["H1"]),
            "cost_model": {
                "type": "per_market",
                "markets": {
                    "GBPUSD": {
                        "spread_points": "5",
                        "swap_long_per_lot": "-5",
                        "swap_short_per_lot": "-5",
                    }
                },
            },
        }

        launched = client.post("/sweeps", json=body)

        assert launched.status_code == 202, launched.text
        (row,) = client.get(f"/sweeps/{launched.json()['id']}").json()["runs"]
        assert row["run"]["cost_model"] == {
            "type": "spread_commission",
            "spread_points": "5",
            "commission_per_unit": "0",
            "swap": {"long_per_lot": "-5", "short_per_lot": "-5"},
        }

    def test_a_swap_that_is_not_a_number_is_refused(self, client: Any) -> None:
        entry = an_entry(client, name=f"bad swap {uuid.uuid4()}")
        body = {
            **a_sweep_body([entry], ["GBPUSD"], ["H1"]),
            "cost_model": {
                "type": "per_market",
                "markets": {"GBPUSD": {"spread_points": "5", "swap_long_per_lot": "a lot"}},
            },
        }

        refused = client.post("/sweeps", json=body)

        assert refused.status_code == 422
        assert "cost model cannot be charged" in refused.json()["detail"]
