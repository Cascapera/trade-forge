"""What the collection transitions decide, independently of the SQL that carries them.

The SQL is proved against a real Postgres in `apps/api/tests/test_collections_integration.py`.
What is proved here is the reasoning: that a status is never allowed to contradict its own
error, and that a row deleted mid-flight stops the collection instead of the worker.

⚠️ The fake below implements only `get`, `add` and `flush`, and that is deliberate. A fake with
more surface than the code uses is a fake that can agree with an implementation the real
`Session` would reject — this project has already been bitten by a fake that agreed with both
the right code and the wrong code, and only the real thing could tell them apart.
"""

import datetime as dt
import uuid
from typing import Any

import pytest

from tradeforge_db.collections import (
    create_collection,
    finish_collection,
    read_collection,
    record_progress,
    start_collection,
)
from tradeforge_db.models import BacktestStatus, Collection
from tradeforge_engine.domain import AssetClass

NOW = dt.datetime(2026, 8, 20, 9, 30, tzinfo=dt.UTC)
FROM = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
TO = dt.datetime(2022, 1, 1, tzinfo=dt.UTC)


class FakeSession:
    """Rows in a dict, keyed by id — the whole surface these five functions touch."""

    def __init__(self, *rows: Collection) -> None:
        self.rows: dict[uuid.UUID, Collection] = {row.id: row for row in rows}
        self.added: list[Collection] = []

    def get(self, _model: type[Any], key: uuid.UUID) -> Collection | None:
        return self.rows.get(key)

    def add(self, row: Collection) -> None:
        self.added.append(row)

    def flush(self) -> None:
        """The real one assigns the primary key here, which is what `create_collection` needs
        before it can return the row. Assigning it is the fake being faithful, not helpful."""
        for row in self.added:
            # `getattr`, not `row.id`: the column is typed non-optional, and before a flush it
            # genuinely is None. Reading it the declared way makes the branch unreachable to a
            # type checker while staying reachable to the program.
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()
            self.rows[row.id] = row


def a_row(**fields: object) -> Collection:
    defaults: dict[str, object] = {
        "id": uuid.uuid4(),
        "symbol": "EURUSD",
        "timeframe": "H1",
        "date_from": FROM,
        "date_to": TO,
        "status": BacktestStatus.QUEUED,
        "years_done": 0,
        "years_total": 3,
    }
    defaults.update(fields)
    return Collection(**defaults)


class TestCreating:
    def test_a_new_request_starts_queued_with_no_outcome(self) -> None:
        session: Any = FakeSession()

        created = create_collection(
            session,
            symbol="EURUSD",
            timeframe="H1",
            date_from=FROM,
            date_to=TO,
            asset_class=None,
            years_total=3,
        )

        assert created.status == BacktestStatus.QUEUED
        assert created.years_done == 0
        assert created.years_total == 3
        assert created.candles is None, "a request that has not run has claimed nothing"

    def test_the_supplied_class_is_recorded_as_provenance(self) -> None:
        """⚠️ Stored only when a person supplied it. NULL means the symbol's path decided, and
        the difference is what lets a later reader tell an inherited derivation from an answer.
        """
        session: Any = FakeSession()

        created = create_collection(
            session,
            symbol="XAUUSD",
            timeframe="H1",
            date_from=FROM,
            date_to=TO,
            asset_class=AssetClass.FUTURE,
            years_total=3,
        )

        assert created.asset_class == AssetClass.FUTURE


class TestTheTransitions:
    def test_starting_marks_it_running_and_stamps_the_time(self) -> None:
        row = a_row()
        session: Any = FakeSession(row)

        start_collection(session, row.id, at=NOW)

        assert row.status == BacktestStatus.RUNNING
        assert row.started_at == NOW

    def test_progress_advances_the_year_count(self) -> None:
        row = a_row()
        session: Any = FakeSession(row)

        record_progress(session, row.id, years_done=2)

        assert row.years_done == 2

    def test_finishing_without_an_error_is_done(self) -> None:
        row = a_row(status=BacktestStatus.RUNNING)
        session: Any = FakeSession(row)

        finish_collection(session, row.id, at=NOW, candles=6150, gaps=52)

        assert row.status == BacktestStatus.DONE
        assert row.error is None
        assert (row.candles, row.gaps) == (6150, 52)
        assert row.finished_at == NOW

    def test_finishing_with_an_error_is_failed(self) -> None:
        row = a_row(status=BacktestStatus.RUNNING)
        session: Any = FakeSession(row)

        finish_collection(session, row.id, at=NOW, error="the broker returned no bars")

        assert row.status == BacktestStatus.FAILED
        assert row.error == "the broker returned no bars"

    def test_a_failure_reports_no_counts(self) -> None:
        """⚠️ The status is *derived* from the error rather than passed beside it, and this is
        the pair that derivation makes impossible: a row that says `failed` while reporting six
        thousand candles, or `done` while carrying a reason nobody can act on."""
        row = a_row(status=BacktestStatus.RUNNING, candles=99, gaps=1)
        session: Any = FakeSession(row)

        finish_collection(session, row.id, at=NOW, error="nothing there")

        assert row.status == BacktestStatus.FAILED
        assert row.candles is None, "a stale count from an earlier attempt must not survive"
        assert row.gaps is None


class TestARowThatIsGone:
    """⚠️ Deleted between the 202 and the pickup — a request somebody cancelled.

    Every transition returns quietly rather than raising, because the agent runs one job at a
    time (`WorkerSettings.max_jobs = 1`): an exception here would take down the worker and with
    it every collection queued behind this one, over a row nobody wants any more.
    """

    @pytest.mark.parametrize(
        "transition",
        [
            lambda session, key: start_collection(session, key, at=NOW),
            lambda session, key: record_progress(session, key, years_done=1),
            lambda session, key: finish_collection(session, key, at=NOW, candles=1, gaps=0),
            lambda session, key: finish_collection(session, key, at=NOW, error="gone"),
        ],
    )
    def test_it_is_a_no_op_rather_than_an_exception(self, transition: Any) -> None:
        session: Any = FakeSession()

        transition(session, uuid.uuid4())  # must not raise

    def test_reading_it_back_is_none(self) -> None:
        session: Any = FakeSession()

        assert read_collection(session, uuid.uuid4()) is None
