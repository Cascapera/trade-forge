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
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Dataset,
    Instrument,
    Sweep,
)
from tradeforge_engine.domain import AssetClass

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
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
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

    def test_a_sweep_over_the_cap_is_refused_with_its_own_size(
        self, client: Any, queue: _CapturingQueue
    ) -> None:
        # ⚠️ Refused whole, never trimmed. Half a sweep is a picture of a space that was never
        # searched, and it looks exactly like a picture of one that was.
        entry = an_entry(
            client,
            name=f"huge {uuid.uuid4()}",
            # 300 points, which is inside `MAX_POINTS` for the entry itself — the sweep is over
            # the cap only once the markets and the charts multiply it. That is the shape worth
            # testing: each axis looks reasonable and the product does not.
            grid={"setup.params.period": list(range(3, 303))},
        )

        refused = client.post(
            "/sweeps", json=a_sweep_body([entry], list(SYMBOLS), ["M15", "H1", "H4", "D1"])
        )

        assert refused.status_code == 422
        # 300 x 3 markets x 4 charts, said out loud in the message.
        assert "3600" in refused.text
        assert queue.jobs == []

        # ⚠️ **And the screen says the same thing the button does.** The sentence used to be
        # written out in both endpoints; a rewording of either would have left a person reading
        # one verdict in the preview and getting another from the launch, with nothing failing.
        previewed = client.post(
            "/sweeps/preview",
            json={
                "entry_ids": [entry],
                "symbols": list(SYMBOLS),
                "timeframes": ["M15", "H1", "H4", "D1"],
                "date_from": START.isoformat(),
                "date_to": (START + 100 * HOUR).isoformat(),
            },
        )

        assert previewed.status_code == 200
        assert previewed.json()["error"] == refused.json()["detail"]

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

    def test_a_market_with_no_candles_in_the_window_refuses_the_whole_sweep(
        self, client: Any, session: Session, queue: _CapturingQueue
    ) -> None:
        entry = an_entry(client, name=f"real {uuid.uuid4()}")
        # Move USDJPY's coverage entirely outside the window the body asks for.
        for dataset in session.scalars(
            select(Dataset).join(Instrument).where(Instrument.symbol == "USDJPY")
        ):
            dataset.date_from = START + dt.timedelta(days=3650)
            dataset.date_to = START + dt.timedelta(days=4015)
        session.commit()

        refused = client.post("/sweeps", json=a_sweep_body([entry], list(SYMBOLS), ["M15"]))

        assert refused.status_code == 422
        assert "USDJPY M15" in refused.text
        # ⚠️ Refused whole, and nothing enqueued. The other two markets do have data — running
        # them and silently dropping the third would draw a map of a space it never searched.
        assert queue.jobs == []

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

        assert preview["error"] is not None
        (uncovered,) = preview["uncovered"]
        assert uncovered["symbol"] == "GBPUSD"
        # The range it *does* hold, so the reader can move the window rather than guess at it.
        assert uncovered["covers"] is not None
        # And the launch agrees, which is the whole point of a preview.
        assert client.post("/sweeps", json=body).status_code == 422


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
