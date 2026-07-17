"""`tradeforge-collector`, driven from the command line — mock source, no database."""

import datetime as dt
from pathlib import Path

import pytest

from tradeforge_collector import cli
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
