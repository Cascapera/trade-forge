"""`tradeforge-collector`, driven from the command line — mock source, no database."""

import datetime as dt
from pathlib import Path

import pytest

# Importable on Linux: `mt5_source` defers its `MetaTrader5` import until `connect()`.
from tradeforge_collector import cli, mt5_source
from tradeforge_collector.storage import read_candles


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
