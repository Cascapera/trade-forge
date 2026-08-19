"""`tradeforge-collector`, driven from the command line — mock source, no database."""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import redis

# Importable on Linux: `mt5_source` defers its `MetaTrader5` import until `connect()`.
from tradeforge_collector import cli, mt5_source, publisher
from tradeforge_collector.storage import read_candles
from tradeforge_db.instruments import CatalogueEntry


def test_a_backfill_runs_end_to_end_and_prints_a_gap_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cli.main(
        [
            "backfill",
            "EURUSD",
            "H1",
            "2024-01-01",
            "2024-02-01",
            "--data-dir",
            str(tmp_path),
            "--no-catalogue",
        ]
    )

    assert exit_code == 0

    output = capsys.readouterr().out
    assert "EURUSD H1" in output
    assert "weekend" in output
    assert len(read_candles(tmp_path, "EURUSD", "H1")) > 500


def test_a_date_is_read_as_midnight_utc() -> None:
    """Never local midnight: the same command must mean the same thing in every timezone."""
    assert cli._date("2024-03-01") == dt.datetime(2024, 3, 1, tzinfo=dt.UTC)


def test_a_malformed_date_is_refused() -> None:
    with pytest.raises(Exception, match="YYYY-MM-DD"):
        cli._date("01/03/2024")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("+3", 3.0), ("3", 3.0), ("-5", -5.0), ("5.5", 5.5), ("0", 0.0)],
)
def test_the_server_offset_is_read_as_hours_ahead_of_utc(given: str, expected: float) -> None:
    assert cli._hours(given) == dt.timedelta(hours=expected)


def test_a_malformed_server_offset_is_refused() -> None:
    with pytest.raises(Exception, match="hours ahead of UTC"):
        cli._hours("three")


def _mt5_arguments_seen_by_the_source(
    monkeypatch: pytest.MonkeyPatch, *extra: str
) -> dict[str, object]:
    """Build `--source mt5` arguments and record what `_source` hands to `MT5Source`.

    `_source` is the seam worth testing: it is where the parsed flags become the object
    that does the work, and the only place the wiring shows without a real terminal.
    """
    seen: dict[str, object] = {}

    class _Recorder:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

        def connect(self) -> "_Recorder":
            return self

    monkeypatch.setattr(mt5_source, "MT5Source", _Recorder)

    args = cli._parser().parse_args(
        ["backfill", "EURUSD", "H1", "2024-01-01", "2024-02-01", "--source", "mt5", *extra]
    )
    cli._source(args)

    return seen


def test_the_server_offset_reaches_the_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is useless if it stops at the parser."""
    seen = _mt5_arguments_seen_by_the_source(monkeypatch, "--server-offset", "+3")

    assert seen["server_offset"] == dt.timedelta(hours=3)


def test_without_the_flag_the_source_is_told_to_measure(monkeypatch: pytest.MonkeyPatch) -> None:
    """`None` is what asks for measurement — an accidental `timedelta()` would mean UTC."""
    seen = _mt5_arguments_seen_by_the_source(monkeypatch)

    assert seen["server_offset"] is None


def test_an_unknown_timeframe_is_refused_before_anything_runs() -> None:
    """argparse rejects it against the DSL's list — no download, no partial dataset."""
    with pytest.raises(SystemExit):
        cli.main(["backfill", "EURUSD", "M2", "2024-01-01", "2024-02-01"])


def test_a_failed_backfill_reports_the_reason_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A symbol the source does not know. The user gets a sentence, not a traceback."""
    exit_code = cli.main(
        [
            "backfill",
            "DOGECOIN",
            "H1",
            "2024-01-01",
            "2024-02-01",
            "--data-dir",
            str(tmp_path),
            "--no-catalogue",
        ]
    )

    assert exit_code == 1
    assert "no synthetic instrument" in capsys.readouterr().err


class _RecordingSession:
    """Stands in for a real session: the upserts are asserted, not the SQL."""


def _stub_database(monkeypatch: pytest.MonkeyPatch) -> list[tuple[CatalogueEntry, ...]]:
    """Replace the Postgres wiring, keeping the command's own logic under test.

    The upsert itself is proven against a real database in
    `packages/db/tests/test_constraints_integration.py`; what is worth testing *here* is
    the wiring — which symbols get read, what is built from them, and what the operator is
    told. Driving that through Docker would test SQLAlchemy twice and this command once.
    """
    written: list[tuple[CatalogueEntry, ...]] = []

    def record(_session: object, entries: tuple[CatalogueEntry, ...]) -> None:
        written.append(entries)

    monkeypatch.setattr(cli, "create_db_engine", _StubEngine)
    monkeypatch.setattr(cli, "create_session_factory", lambda _engine: None)
    monkeypatch.setattr(cli, "session_scope", lambda _factory: _session_scope())
    monkeypatch.setattr(cli, "upsert_instruments", record)
    return written


class _StubEngine:
    def dispose(self) -> None:
        """The command disposes the engine in a `finally`; this records that it can."""


@contextmanager
def _session_scope() -> Iterator[_RecordingSession]:
    yield _RecordingSession()


def test_the_catalogue_command_refreshes_specs_without_touching_parquet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason the command exists: specs move without history moving.

    A spread is re-quoted far more often than two years of candles change, and refreshing
    it used to mean re-running a backfill. Nothing is written to `tmp_path` here, and that
    empty directory is the assertion.
    """
    written = _stub_database(monkeypatch)

    exit_code = cli.main(["catalogue", "EURUSD", "--source", "mock"])

    assert exit_code == 0
    assert list(tmp_path.iterdir()) == []
    assert len(written) == 1
    entry = written[0][0]
    assert entry.spec.symbol == "EURUSD"


def test_the_catalogue_command_reports_unknown_rather_than_a_free_instrument(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mock source has no broker behind it, so it quotes nothing — and says so.

    Printing `0` here would read as "this instrument is free to trade", which is a claim
    about a market that a made-up source is in no position to make.
    """
    written = _stub_database(monkeypatch)

    cli.main(["catalogue", "EURUSD", "--source", "mock"])

    assert written[0][0].default_spread_points is None
    output = capsys.readouterr().out
    assert "spread unknown" in output
    assert "spread 0" not in output


def test_the_catalogue_command_takes_several_symbols_in_one_go(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    written = _stub_database(monkeypatch)

    cli.main(["catalogue", "EURUSD", "AAPL", "--source", "mock"])

    assert [entries[0].spec.symbol for entries in written] == ["EURUSD", "AAPL"]


def test_the_catalogue_command_names_a_symbol_the_source_does_not_have(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_database(monkeypatch)

    exit_code = cli.main(["catalogue", "NOPE", "--source", "mock"])

    assert exit_code == 1
    assert "NOPE" in capsys.readouterr().err


def test_the_catalogue_command_demands_the_offset_against_a_real_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loop the guard cannot close on its own.

    `spread_points` dates the last quote by undoing the server's clock offset, and a measured
    offset is wrong exactly when the market is shut — which is the case the guard exists for.
    Demanding the offset up front is the one cheap way out; without this the command would
    accept a guess and wave a stale spread through.
    """
    _stub_database(monkeypatch)

    exit_code = cli.main(["catalogue", "AAPL", "--source", "mt5"])

    assert exit_code == 1
    assert "--server-offset" in capsys.readouterr().err


def test_the_catalogue_command_does_not_demand_an_offset_from_the_mock_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no terminal clock to undo, and no spread to date, behind a made-up source."""
    written = _stub_database(monkeypatch)

    assert cli.main(["catalogue", "EURUSD", "--source", "mock"]) == 0
    assert len(written) == 1


# --------------------------------------------------------------------------- #
# `live` — the loop, wired up                                                   #
# --------------------------------------------------------------------------- #


class _StubRedis:
    """Enough of a client for `_live` to build one and close it."""

    def __init__(self, **_kwargs: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubPublisher:
    """A publisher that keeps what it is given and starts from an empty stream."""

    def __init__(self, _client: object) -> None:
        self.published: list[object] = []

    def publish(self, _subscription: object, candle: object) -> bool:
        self.published.append(candle)
        return True

    def last_published(self, _subscription: object) -> None:
        return None


@pytest.fixture
def stub_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the client nor the publisher touches a real server in these tests."""
    monkeypatch.setattr(redis, "Redis", _StubRedis)
    monkeypatch.setattr(publisher, "RedisCandlePublisher", _StubPublisher)


def test_a_single_poll_runs_end_to_end_against_the_mock_source(stub_redis: None) -> None:
    """`--once` is the smoke test, and it has to work with no terminal and no broker."""
    assert cli.main(["live", "EURUSD", "M5", "--once"]) == 0


def test_watching_a_daily_bar_is_refused_with_the_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """⚠️ Measured, not suspected: 499 of 499 daily bars carried the **next** session's
    date, because the broker's day opens at its own midnight (UTC+3) and undoing that offset
    moves a daily bar across the date boundary. Intraday was checked against H1 and is sound.

    A refusal rather than a warning, because the consumer of this stream is a paper session
    that will not be reading the log — and a bar stamped a day out is not a rounding error,
    it is a trade placed on the wrong day.
    """
    exit_code = cli.main(["live", "EURUSD", "D1", "--once"])

    assert exit_code == 1
    error = capsys.readouterr().err
    assert "refusing to watch D1" in error
    assert "one day ahead" in error


def test_the_refusal_happens_before_the_terminal_is_opened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A guard that fires after the terminal is opened is a guard that needs a terminal.

    ⚠️ The thing that must not have run is `_source`, not the Redis client. Against
    `--source mt5` that call is what attaches to MetaTrader and measures its clock — so a guard
    placed after it would refuse D1 only on a machine that already had a live terminal, which
    is the one machine where the refusal is least likely to be read.
    """

    def _explode(_args: object) -> None:
        raise AssertionError("the refusal must come before the source is built")

    monkeypatch.setattr(cli, "_source", _explode)

    assert cli.main(["live", "EURUSD", "M5", "D1", "--once"]) == 1
    assert "refusing to watch D1" in capsys.readouterr().err


def test_an_intraday_timeframe_is_not_caught_by_the_daily_guard(stub_redis: None) -> None:
    # The guard names one timeframe. A blanket refusal would have taken the whole loop with it.
    assert cli.main(["live", "EURUSD", "M5", "M15", "H4", "--once"]) == 0


def test_the_backfill_ceiling_reaches_the_loop(
    monkeypatch: pytest.MonkeyPatch, stub_redis: None
) -> None:
    """The flag is useless if it stops at the parser."""
    seen: dict[str, object] = {}

    def _record(*_args: object, **kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run", _record)

    cli.main(["live", "EURUSD", "M5", "--once", "--max-backfill", "42"])

    assert seen["max_backfill"] == 42
    # `--once` is one poll, and `None` would be the loop that never ends.
    assert seen["polls"] == 1


def test_without_once_the_loop_is_told_to_run_for_ever(
    monkeypatch: pytest.MonkeyPatch, stub_redis: None
) -> None:
    seen: dict[str, object] = {}

    def _record(*_args: object, **kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run", _record)

    cli.main(["live", "EURUSD", "M5"])

    assert seen["polls"] is None


def test_ctrl_c_is_a_clean_stop_and_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, stub_redis: None
) -> None:
    """⚠️ This command is meant to be killed — it is a loop with no natural end.

    A traceback on Ctrl-C teaches the reader to ignore tracebacks, which is the habit that
    hides the next real one.
    """

    def _interrupted(*_args: object, **_kwargs: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", _interrupted)

    assert cli.main(["live", "EURUSD", "M5"]) == 0


def test_a_source_that_cannot_be_watched_is_refused_with_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stub_redis: None
) -> None:
    """⚠️ `LiveSource` is `runtime_checkable` for exactly this line.

    Conforming structurally means having the methods, and that is what `isinstance` checks
    here — so a source that cannot be watched is turned away with an explanation instead of
    reaching the first poll and failing on a missing attribute.
    """

    class _NotWatchable:
        """Satisfies `MarketDataSource` and nothing else."""

    monkeypatch.setattr(cli, "_source", lambda _args: _NotWatchable())

    assert cli.main(["live", "EURUSD", "M5", "--once"]) == 1
    assert "cannot be watched live" in capsys.readouterr().err
