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

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.models import Dataset, Instrument
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

        assert client.delete(f"/catalog/{entry}").status_code == 204

        read = client.get(f"/sweeps/{launched.json()['id']}")
        assert read.status_code == 200
        assert len(read.json()["runs"]) == 1


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
