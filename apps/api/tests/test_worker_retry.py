"""The worker when the database will not answer: hand the job back, and only the last try fails.

No Postgres here, on purpose — the case under test is a Postgres that is not there. The sessions
are fakes that raise what psycopg raises through SQLAlchemy while the server is starting.
"""

import asyncio
import uuid
from typing import Any

import pytest
from arq.worker import Function, Retry
from sqlalchemy.exc import IntegrityError, OperationalError

from tradeforge_api.worker import (
    MAX_TRIES,
    WorkerSettings,
    database_unreachable,
    retry_delay,
    run_backtest,
)
from tradeforge_db.models import BacktestStatus

RUN = uuid.UUID(int=7)


def starting_up() -> OperationalError:
    return OperationalError(
        "SELECT backtests",
        {},
        Exception("FATAL: the database system is starting up"),
    )


class _Row:
    def __init__(self) -> None:
        self.status = BacktestStatus.QUEUED
        self.error: str | None = None
        self.started_at: Any = None
        self.finished_at: Any = None


class _DriverError(Exception):
    """A driver error carrying the SQLSTATE psycopg would attach."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(f"sqlstate {sqlstate}")
        self.sqlstate = sqlstate


def answered(sqlstate: str) -> OperationalError:
    return OperationalError("SELECT backtests", {}, _DriverError(sqlstate))


class _DeadSession:
    """Every read fails the way a refused connection does; rollback fails the same way."""

    def __init__(self) -> None:
        self.closed = False
        self.rolled_back = False

    def get(self, *_args: object) -> object:
        raise starting_up()

    def rollback(self) -> None:
        self.rolled_back = True
        raise starting_up()

    def close(self) -> None:
        self.closed = True


class _LiveSession:
    """Answers with one row and records the commit."""

    def __init__(self, row: _Row) -> None:
        self.row = row
        self.commits = 0
        self.closed = False

    def get(self, *_args: object) -> _Row:
        return self.row

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class _Redis:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish(self, _channel: str, message: str) -> None:
        self.events.append(message)


def context(job_try: int, sessions: list[Any]) -> dict[str, Any]:
    handed: list[Any] = []

    def factory() -> Any:
        # ⚠️ A new session is only handed out once every earlier one has been let go. The old one
        # may hold this row's lock inside an aborted transaction, and the new one's UPDATE would
        # wait for it for ever, inside the same coroutine.
        assert all(one.closed for one in handed), "a session was opened while another was held"
        handed.append(sessions[len(handed)])
        return handed[-1]

    return {
        "job_try": job_try,
        "session_factory": factory,
        "settings": type("S", (), {"parquet_root": "unused"})(),
        "redis": _Redis(),
    }


class TestAnUnreachableDatabase:
    @pytest.mark.parametrize("job_try", [1, MAX_TRIES - 1])
    def test_hands_the_job_back_before_the_last_try(self, job_try: int) -> None:
        dead = _DeadSession()
        ctx = context(job_try, [dead])

        with pytest.raises(Retry) as raised:
            asyncio.run(run_backtest(ctx, str(RUN)))

        # arq keeps the delay in milliseconds.
        assert raised.value.defer_score == retry_delay(job_try) * 1000
        assert isinstance(raised.value.__cause__, OperationalError)
        assert dead.closed
        assert ctx["redis"].events == []

    def test_records_the_run_as_failed_on_the_last_try(self) -> None:
        row = _Row()
        dead, live = _DeadSession(), _LiveSession(row)
        ctx = context(MAX_TRIES, [dead, live])

        asyncio.run(run_backtest(ctx, str(RUN)))

        assert row.status is BacktestStatus.FAILED
        # The reason a reader needs: the database, in the driver's own words.
        assert "the database system is starting up" in (row.error or "")
        assert row.started_at is not None
        assert row.finished_at is not None
        assert live.commits == 1
        # Both sessions given back, the dead one first and rolled back: see `context`.
        assert dead.closed
        assert dead.rolled_back
        assert live.closed
        assert any('"failed"' in event for event in ctx["redis"].events)

    def test_lets_arq_keep_the_error_when_even_the_last_write_cannot_land(self) -> None:
        dead, still_dead = _DeadSession(), _DeadSession()
        ctx = context(MAX_TRIES, [dead, still_dead])

        with pytest.raises(OperationalError):
            asyncio.run(run_backtest(ctx, str(RUN)))

        assert still_dead.closed

    def test_an_error_the_database_answered_is_recorded_on_the_first_try(self) -> None:
        # A cancelled query reached the server and came back: retrying would re-run the whole
        # backtest to get the same answer, and raising it would leave the row queued for ever.
        class _Cancelled(_DeadSession):
            def get(self, *_args: object) -> object:
                raise answered("57014")

            def rollback(self) -> None:
                self.rolled_back = True

        row = _Row()
        cancelled, live = _Cancelled(), _LiveSession(row)
        ctx = context(1, [cancelled, live])

        asyncio.run(run_backtest(ctx, str(RUN)))

        assert row.status is BacktestStatus.FAILED
        assert "sqlstate 57014" in (row.error or "")
        assert cancelled.closed
        assert live.closed

    def test_the_last_try_never_turns_a_finished_run_into_a_failed_one(self) -> None:
        # The final commit landed but its answer was lost with the connection.
        row = _Row()
        row.status = BacktestStatus.DONE
        dead, live = _DeadSession(), _LiveSession(row)

        asyncio.run(run_backtest(context(MAX_TRIES, [dead, live]), str(RUN)))

        assert row.status is BacktestStatus.DONE
        assert row.error is None
        assert row.finished_at is None
        assert live.commits == 0

    def test_an_error_that_is_not_the_database_is_not_retried(self) -> None:
        class _Broken(_DeadSession):
            def get(self, *_args: object) -> object:
                raise RuntimeError("not a connection problem")

        with pytest.raises(RuntimeError):
            asyncio.run(run_backtest(context(1, [_Broken()]), str(RUN)))


class TestWhatCountsAsUnreachable:
    @pytest.mark.parametrize("sqlstate", ["08006", "08001", "57P01", "57P02", "57P03"])
    def test_a_connection_that_failed_or_a_server_that_is_going_or_coming(
        self, sqlstate: str
    ) -> None:
        assert database_unreachable(answered(sqlstate))

    @pytest.mark.parametrize("sqlstate", ["57014", "40P01", "53100", "54000", "55P03"])
    def test_an_answer_from_a_server_that_is_there(self, sqlstate: str) -> None:
        # Cancelled query, deadlock, disk full, program limit, lock not available.
        assert not database_unreachable(answered(sqlstate))

    def test_a_refusal_before_the_server_said_anything(self) -> None:
        assert database_unreachable(starting_up())

    def test_an_invalidated_connection_whatever_the_code(self) -> None:
        exc = answered("57014")
        exc.connection_invalidated = True

        assert database_unreachable(exc)

    def test_not_an_operational_error_and_not_a_database_error(self) -> None:
        assert not database_unreachable(IntegrityError("INSERT", {}, _DriverError(None)))
        assert not database_unreachable(RuntimeError("boom"))


class TestTheBudget:
    def test_the_waits_grow_and_add_up_to_140_seconds(self) -> None:
        waits = [retry_delay(job_try) for job_try in range(1, MAX_TRIES)]

        assert waits == [5, 10, 15, 20, 25, 30, 35]
        assert sum(waits) == 140

    def test_arq_is_told_the_same_number_of_tries_the_job_counts(self) -> None:
        # If arq's limit were lower, it would refuse the job before our last try could record
        # the failure — and the row would stay queued, the bug this module exists to close.
        registered = {
            one.name: one for one in WorkerSettings.functions if isinstance(one, Function)
        }
        assert registered["run_backtest"].max_tries == MAX_TRIES
        # Stated on that job alone: the walk-forward keeps arq's default.
        assert "run_walk_forward" not in registered
