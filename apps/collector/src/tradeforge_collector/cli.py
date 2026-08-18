"""`tradeforge-collector` — download history into Parquet and catalogue it.

    tradeforge-collector backfill EURUSD H1 2024-01-01 2024-12-31
    tradeforge-collector backfill EURUSD H1 2024-01-01 2024-12-31 --source mt5

`--source mock` is the default, and that is a deliberate choice: the command a new
contributor runs first must work on their machine, on Linux, with no broker account.
`--source mt5` is the one that needs Windows, a terminal that is open and logged in,
and a symbol the broker actually offers.

With the market closed, `--source mt5` also needs `--server-offset`: the broker's clock
is otherwise read from the newest tick, and a shut market freezes that tick. The
collector refuses rather than guess — see `mt5_source.offset_is_plausible`.
"""

import argparse
import datetime as dt
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from tradeforge_collector.backfill import backfill
from tradeforge_collector.gaps import format_report
from tradeforge_collector.live import LiveSource, Subscription, poll_once
from tradeforge_collector.source import MarketDataSource
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_collector.timeframes import TIMEFRAME_STEP
from tradeforge_db.instruments import CatalogueEntry, upsert_instruments
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope
from tradeforge_engine.domain import AssetClass

DEFAULT_DATA_DIR = Path("data/ohlcv")


def _date(value: str) -> dt.datetime:
    """A calendar date on the command line is midnight UTC. Never local midnight."""
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _hours(value: str) -> dt.timedelta:
    """`+3`, `-5`, `5.5` — hours ahead of UTC. Some timezones are half hours."""
    try:
        return dt.timedelta(hours=float(value))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected hours ahead of UTC, like +3 or -5.5, got {value!r}"
        ) from None


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
        "--server-offset",
        type=_hours,
        metavar="HOURS",
        help=(
            "the broker's clock, in hours ahead of UTC (e.g. +3). Otherwise it is measured "
            "from the newest tick — which only works while the market is open, because a "
            "closed market freezes that tick"
        ),
    )
    fill.add_argument(
        "--no-catalogue",
        action="store_true",
        help="write the Parquet but do not touch Postgres",
    )

    # Re-reading a contract specification used to mean re-running a backfill, which downloads
    # thousands of candles to refresh a handful of fields. Broker specs change on their own
    # schedule — a spread is re-quoted far more often than history is re-downloaded — so
    # refreshing them deserves a command that does not touch Parquet at all.
    catalogue = commands.add_parser(
        "catalogue",
        help="refresh contract specs (including the spread) without downloading candles",
    )
    catalogue.add_argument("symbols", nargs="+", metavar="SYMBOL")
    catalogue.add_argument(
        "--source",
        choices=("mock", "mt5"),
        default="mt5",
        help="mt5: a real terminal (default, since specs are what a terminal knows)",
    )
    catalogue.add_argument(
        "--asset-class",
        choices=[member.value for member in AssetClass],
        help="override the class inferred from the symbol's MT5 path",
    )
    catalogue.add_argument(
        "--server-offset",
        type=_hours,
        metavar="HOURS",
        help=(
            "the broker's clock, in hours ahead of UTC (e.g. +3). REQUIRED with --source mt5: "
            "the spread guard dates the last quote by undoing this offset, and a closed "
            "market is exactly what corrupts the measured one"
        ),
    )

    # The loop the whole of phase 3 hangs from. It has no natural end, so it is a command that
    # is expected to be killed — see `_live` for why Ctrl-C is a clean exit here.
    live = commands.add_parser(
        "live",
        help="watch a symbol and publish each closed candle to a Redis stream",
        description=(
            "Polls the source for the bar that just closed and publishes it to "
            "candles.{symbol}.{timeframe}. It never works out 'closed' from a clock: the "
            "source is asked for the closed bar by position, because a broker's offset is "
            "measured from the newest tick and a shut market freezes that measurement."
        ),
    )
    live.add_argument("symbol")
    live.add_argument(
        "timeframes",
        nargs="+",
        choices=sorted(TIMEFRAME_STEP),
        metavar="TIMEFRAME",
        help="one or more timeframes to watch on this symbol, e.g. M1 M5",
    )
    live.add_argument(
        "--source",
        choices=("mock", "mt5"),
        default="mock",
        help="mock: deterministic synthetic data (default). mt5: a real terminal (Windows)",
    )
    live.add_argument(
        "--asset-class",
        choices=[member.value for member in AssetClass],
        help="override the class inferred from the symbol's MT5 path",
    )
    live.add_argument(
        "--server-offset",
        type=_hours,
        metavar="HOURS",
        help=(
            "the broker's clock, in hours ahead of UTC (e.g. +3). Only labels the candle here "
            "— whether a bar closed is read from its position, never from this"
        ),
    )
    live.add_argument(
        "--every",
        type=float,
        default=5.0,
        # Far shorter than the shortest bar, because a close is only noticed on the poll after
        # it happens: five seconds on M1 means a bar is announced within 5s of closing.
        help="seconds between polls (default: 5)",
    )
    live.add_argument(
        "--once",
        action="store_true",
        help="poll a single time and exit — what a smoke test runs",
    )
    live.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "localhost"))
    live.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))

    return parser


def _catalogue_command(args: argparse.Namespace) -> int:
    """Upsert contract specs for the named symbols. No Parquet, no candles.

    `--server-offset` is mandatory against a real terminal, and the reason is circular in a
    way worth stating. The spread guard refuses a quote older than a few minutes, and it dates
    the quote by undoing the server's clock offset. When that offset is *measured* rather than
    stated, it comes from the newest tick — so a closed market, the very thing the guard
    exists to detect, is what makes the offset wrong. Measured on this project's terminal: 47
    minutes after the US close the offset read +2 h against the true +3, which makes a
    47-minute-old quote look one minute old and waves the stale spread straight through.

    Demanding the offset up front costs one flag and closes the loop. The backfill does not
    demand it because there `offset_is_plausible` can still refuse the gross case; here the
    dangerous case is a short closure, which is precisely the size that slips under it.
    """
    if args.source == "mt5" and args.server_offset is None:
        raise ValueError(
            "catalogue needs --server-offset with --source mt5 (for example --server-offset "
            "+3): the spread guard dates the last quote with it, and a measured offset is "
            "wrong exactly when the market is shut, which is when the guard has to bite"
        )

    source = _source(args)
    engine = create_db_engine()
    try:
        with session_scope(create_session_factory(engine)) as session:
            for symbol in args.symbols:
                spec = source.instrument(symbol)
                spread = source.spread_points(symbol)
                upsert_instruments(session, (CatalogueEntry(spec, spread),))
                # Printed as the pair it is: the number and its unit. A spread means nothing
                # without the tick it is counted in, and "12" alone has burned this project
                # once already on the difference between MT5 points and engine ticks.
                shown = "unknown" if spread is None else f"{spread.normalize()} ticks"
                print(f"{spec.symbol}: tick {spec.tick_size.normalize()}, spread {shown}")
    finally:
        engine.dispose()
    return 0


def _live(args: argparse.Namespace) -> int:
    """Watch one symbol on one or more timeframes until interrupted.

    The source comes through the same `_source` seam the backfill uses, so `--source mock` runs
    this entire loop on a Linux box with no terminal — which is the only reason its behaviour
    can be under test at all.
    """
    # Imported here rather than at module scope so `--help` and the other subcommands never
    # need a Redis client, the same reason `_source` defers the MetaTrader import.
    from redis import Redis  # noqa: PLC0415

    from tradeforge_collector.publisher import RedisCandlePublisher  # noqa: PLC0415

    source = _source(args)
    if not isinstance(source, LiveSource):
        raise ValueError(f"the {args.source} source cannot be watched live")
    subscriptions = [Subscription(args.symbol, timeframe) for timeframe in args.timeframes]

    client = Redis(host=args.redis_host, port=args.redis_port, decode_responses=True)
    publisher = RedisCandlePublisher(client)
    seen: dict[Subscription, dt.datetime] = {}

    logging.getLogger(__name__).info(
        "watching %s on %s, polling every %.0fs",
        args.symbol,
        ", ".join(args.timeframes),
        args.every,
    )
    try:
        while True:
            poll_once(source, publisher, subscriptions, seen=seen)
            if args.once:
                return 0
            time.sleep(args.every)
    except KeyboardInterrupt:
        # ⚠️ A clean stop, not a failure. This command is meant to be killed — it is a loop with
        # no natural end — and a traceback on Ctrl-C teaches the reader to ignore tracebacks.
        logging.getLogger(__name__).info("stopped")
        return 0
    finally:
        client.close()


def _source(args: argparse.Namespace) -> MarketDataSource:
    if args.source == "mock":
        return SyntheticSource()

    # Imported here, not at module scope: on Linux the library does not exist, and this
    # file must stay importable anyway (ADR-02).
    from tradeforge_collector.mt5_source import MT5Source  # noqa: PLC0415 — the ADR-02 boundary

    asset_class = AssetClass(args.asset_class) if args.asset_class else None
    return MT5Source(asset_class=asset_class, server_offset=args.server_offset).connect()


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
        commands = {
            "catalogue": _catalogue_command,
            "live": _live,
            "backfill": _backfill,
        }
        return commands[args.command](args)
    except (LookupError, ValueError, ConnectionError) as error:
        # These are the ways either command legitimately fails: a symbol the broker does not
        # offer, a range with no data, a terminal that is not running. A stack trace
        # would say nothing a user can act on.
        print(f"error: {error}", file=sys.stderr)
        return 1
