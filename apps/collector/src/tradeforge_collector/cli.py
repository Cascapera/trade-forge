"""`tradeforge-collector` — download history into Parquet and catalogue it.

    tradeforge-collector backfill EURUSD H1 2024-01-01 2024-12-31
    tradeforge-collector backfill EURUSD H1 2024-01-01 2024-12-31 --source mt5

`--source mock` is the default, and that is a deliberate choice: the command a new
contributor runs first must work on their machine, on Linux, with no broker account.
`--source mt5` is the one that needs Windows, a terminal that is open and logged in,
and a symbol the broker actually offers.
"""

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from tradeforge_collector.backfill import backfill
from tradeforge_collector.gaps import format_report
from tradeforge_collector.source import MarketDataSource
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_collector.timeframes import TIMEFRAME_STEP
from tradeforge_db.models import AssetClass
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope

DEFAULT_DATA_DIR = Path("data/ohlcv")


def _date(value: str) -> dt.datetime:
    """A calendar date on the command line is midnight UTC. Never local midnight."""
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeforge-collector", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    fill = commands.add_parser("backfill", help="download history into Parquet")
    fill.add_argument("symbol")
    fill.add_argument("timeframe", choices=sorted(TIMEFRAME_STEP))
    fill.add_argument("start", type=_date, metavar="YYYY-MM-DD")
    fill.add_argument("end", type=_date, metavar="YYYY-MM-DD")
    fill.add_argument(
        "--source",
        choices=("mock", "mt5"),
        default="mock",
        help="mock: deterministic synthetic data (default). mt5: a real terminal (Windows)",
    )
    fill.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    fill.add_argument(
        "--asset-class",
        choices=[member.value for member in AssetClass],
        help="override the class inferred from the symbol's MT5 path",
    )
    fill.add_argument(
        "--no-catalogue",
        action="store_true",
        help="write the Parquet but do not touch Postgres",
    )

    return parser


def _source(args: argparse.Namespace) -> MarketDataSource:
    if args.source == "mock":
        return SyntheticSource()

    # Imported here, not at module scope: on Linux the library does not exist, and this
    # file must stay importable anyway (ADR-02).
    from tradeforge_collector.mt5_source import MT5Source  # noqa: PLC0415 — the ADR-02 boundary

    asset_class = AssetClass(args.asset_class) if args.asset_class else None
    return MT5Source(asset_class=asset_class).connect()


def _backfill(args: argparse.Namespace) -> int:
    source = _source(args)

    if args.no_catalogue:
        report = backfill(
            source,
            root=args.data_dir,
            symbol=args.symbol,
            timeframe=args.timeframe,
            start=args.start,
            end=args.end,
        )
    else:
        engine = create_db_engine()
        try:
            with session_scope(create_session_factory(engine)) as session:
                report = backfill(
                    source,
                    root=args.data_dir,
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    start=args.start,
                    end=args.end,
                    session=session,
                )
        finally:
            engine.dispose()

    print(
        f"{report.instrument.symbol} {report.timeframe}: {report.candles} candles "
        f"({report.date_from:%Y-%m-%d} to {report.date_to:%Y-%m-%d}) -> {report.parquet_path}"
    )
    print(format_report(report.gaps))

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a subcommand and return a shell exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    args = _parser().parse_args(argv)
    try:
        return _backfill(args)
    except (LookupError, ValueError, ConnectionError) as error:
        # These are the ways a backfill legitimately fails: a symbol the broker does not
        # offer, a range with no data, a terminal that is not running. A stack trace
        # would say nothing a user can act on.
        print(f"error: {error}", file=sys.stderr)
        return 1
