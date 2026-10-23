"""`tradeforge-ml` — the meta-labeling tools (ADR-0031).

    tradeforge-ml export <sweep-id> [--out data/ml]   # the sweep's event base, in Parquet
    tradeforge-ml features <sweep-id> [--out data/ml] [--ohlcv data/ohlcv]
                                                      # its variables, beside the events
    tradeforge-ml train <sweep-id> [--out data/ml] [--validation 2023-01-01:2025-01-01]
                       [--exclude 2020-03-01:2020-07-01 ...] [--model logistic|boosting]
                       [--min-stop-ticks 5] [--min-stop-spreads 2] [--no-floor]
                                                      # the first meta-label, judged
    tradeforge-ml replay <sweep-id> [--out data/ml] [--ohlcv data/ohlcv] [--horizon 150]
                         [--processes N] [--costs measured|run] [--stops 0.5,0.75,1,1.5,2,3]
                                                      # every proposal, traded alone, and
                                                      # under each stop of the grid
                                                      #   -> sweep=<id>/independent-h150/
    tradeforge-ml grid <sweep-id> [--out data/ml] [--base independent-h150]
                      [--validation 2023-01-01:2025-01-01] [--min-trades 30]
                      [--min-stop-ticks 5] [--min-stop-spreads 2] [--no-floor]
                                                      # the grid's stops and targets, scored:
                                                      # a fixed cell per market, no model

`features` and `train` read the run-based base; `--base independent-h150` points them at the
replayed one instead.

Read-only on the database. Connects with the same `POSTGRES_*` settings as `tradeforge-db`;
`features` reads only each instrument's point there, to put its spread in price, and `train` each
one's tick and splits, for the stop floor.
"""

import argparse
import datetime as dt
import sys
import uuid
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from tradeforge_db.models import Instrument, StockSplit
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_ml.export import export_sweep
from tradeforge_ml.feature_export import write_features
from tradeforge_ml.replay import HORIZON, STOPS
from tradeforge_ml.replay_export import jobs_of, write_independent

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from tradeforge_ml.model import Market


def span(text: str) -> tuple[dt.datetime, dt.datetime]:
    """`2023-01-01:2025-01-01` — from the first day, up to but not including the second."""
    left, right = (
        dt.datetime.fromisoformat(part).replace(tzinfo=dt.UTC) for part in text.split(":")
    )
    if right <= left:
        raise argparse.ArgumentTypeError(f"{text}: the end must come after the start")
    return left, right


def stops(text: str) -> tuple[Decimal, ...]:
    """`0.5,1,2` — the grid's stops, as multiples of the setup's; its own (`1`) among them."""
    try:
        multiples = tuple(Decimal(part) for part in text.split(","))
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"{text}: multiples separated by commas") from error
    if Decimal(1) not in multiples or any(multiple <= 0 for multiple in multiples):
        raise argparse.ArgumentTypeError(f"{text}: positive, and the setup's own stop (1) in it")
    return multiples


def markets_of(session: "Session") -> dict[str, "Market"]:
    """Each instrument's tick and splits, as the stop floor reads them (`model.Market`)."""
    from tradeforge_ml.model import Market  # noqa: PLC0415 — the models are an extra

    splits: dict[str, list[tuple[int, float]]] = {}
    query = (
        select(
            Instrument.symbol,
            StockSplit.effective_on,
            StockSplit.shares_after,
            StockSplit.shares_before,
        )
        .join(Instrument, Instrument.id == StockSplit.instrument_id)
        .order_by(StockSplit.effective_on)
    )
    for symbol, effective_on, after, before in session.execute(query).tuples():
        first_day = dt.datetime.combine(effective_on, dt.time(), tzinfo=dt.UTC)
        splits.setdefault(symbol, []).append(
            (int(first_day.timestamp() * 1_000_000), after / before)
        )
    return {
        symbol: Market(float(tick_size), float(tick_value), tuple(splits.get(symbol, ())))
        for symbol, tick_size, tick_value in session.execute(
            select(Instrument.symbol, Instrument.tick_size, Instrument.tick_value)
        ).tuples()
    }


def _train(args: argparse.Namespace, base: Path) -> int:
    # Here, not at the top: the models are an extra (`tradeforge-ml[models]`), absent from the
    # images, and every other command must run without them.
    from tradeforge_ml.model import Floor, Split  # noqa: PLC0415
    from tradeforge_ml.train import train  # noqa: PLC0415

    start, end = args.validation
    floor = None if args.no_floor else Floor(args.min_stop_ticks, args.min_stop_spreads)
    markets = None
    if floor is not None:
        with create_session_factory(create_db_engine())() as session:
            markets = markets_of(session)
    name, judged = train(base, Split(start, end, tuple(args.exclude)), args.model, floor, markets)
    everything, half = judged.validation[0], judged.validation[2]
    print(
        f"{name}: validation AUC {judged.validation_auc:.3f}, "
        f"mean R all {everything.mean_r:+.3f}, best half {half.mean_r:+.3f}"
        + (
            ""
            if floor is None
            else f" (floor left out {judged.below_floor_training:,} training, "
            f"{judged.below_floor_validation:,} validation)"
        )
    )
    return 0


def _grid(args: argparse.Namespace, base: Path) -> int:
    from tradeforge_ml.grid_report import write_grid  # noqa: PLC0415 — the models are an extra
    from tradeforge_ml.model import Floor, Split  # noqa: PLC0415

    start, end = args.validation
    floor = None if args.no_floor else Floor(args.min_stop_ticks, args.min_stop_spreads)
    markets = None
    if floor is not None:
        with create_session_factory(create_db_engine())() as session:
            markets = markets_of(session)
    name, report = write_grid(
        base, Split(start, end, tuple(args.exclude)), floor, markets, args.min_trades
    )
    print(f"{name}, validation:")
    for rule in report.rules:
        judged = rule.validation
        print(
            f"  {rule.name}: {judged.trades:,} trades, mean R {judged.mean_r:+.3f}, "
            f"total R {judged.total_r:+,.1f}, longest losing run {judged.longest_losing_streak}"
        )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeforge-ml", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write a sweep's event base in Parquet")
    export.add_argument("sweep_id", type=uuid.UUID)
    export.add_argument("--out", type=Path, default=Path("data/ml"))
    features = commands.add_parser("features", help="write the variables of a sweep's events")
    features.add_argument("sweep_id", type=uuid.UUID)
    features.add_argument("--out", type=Path, default=Path("data/ml"))
    features.add_argument("--ohlcv", type=Path, default=Path("data/ohlcv"))
    features.add_argument("--base", default="")
    fit = commands.add_parser("train", help="fit the first meta-label and write its report")
    fit.add_argument("sweep_id", type=uuid.UUID)
    fit.add_argument("--out", type=Path, default=Path("data/ml"))
    fit.add_argument("--validation", type=span, default=span("2023-01-01:2025-01-01"))
    fit.add_argument("--exclude", type=span, action="append", default=[])
    fit.add_argument("--base", default="")
    fit.add_argument("--model", choices=("logistic", "boosting"), default="logistic")
    fit.add_argument("--min-stop-ticks", type=float, default=5.0)
    fit.add_argument("--min-stop-spreads", type=float, default=2.0)
    fit.add_argument("--no-floor", action="store_true", help="keep every entry, as before 14/10")
    again = commands.add_parser("replay", help="replay every entry the sweep tried")
    again.add_argument("sweep_id", type=uuid.UUID)
    again.add_argument("--out", type=Path, default=Path("data/ml"))
    again.add_argument("--ohlcv", type=Path, default=Path("data/ohlcv"))
    again.add_argument("--horizon", type=int, default=HORIZON)
    again.add_argument("--processes", type=int, default=None)
    again.add_argument("--costs", choices=("measured", "run"), default="measured")
    again.add_argument("--stops", type=stops, default=STOPS)
    scored = commands.add_parser("grid", help="score a replayed base's stops and targets")
    scored.add_argument("sweep_id", type=uuid.UUID)
    scored.add_argument("--out", type=Path, default=Path("data/ml"))
    scored.add_argument("--base", default=f"independent-h{HORIZON}")
    scored.add_argument("--validation", type=span, default=span("2023-01-01:2025-01-01"))
    scored.add_argument("--exclude", type=span, action="append", default=[])
    scored.add_argument("--min-trades", type=int, default=30)
    scored.add_argument("--min-stop-ticks", type=float, default=5.0)
    scored.add_argument("--min-stop-spreads", type=float, default=2.0)
    scored.add_argument("--no-floor", action="store_true", help="keep every trade")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    base = args.out / f"sweep={args.sweep_id}" / getattr(args, "base", "")

    if args.command == "train":
        return _train(args, base)
    if args.command == "grid":
        return _grid(args, base)

    factory = create_session_factory(create_db_engine())
    if args.command == "replay":
        with factory() as session:
            jobs = jobs_of(session, args.sweep_id)
        replayed = write_independent(
            jobs,
            sweep_id=str(args.sweep_id),
            out=args.out,
            ohlcv=args.ohlcv,
            horizon=args.horizon,
            processes=args.processes,
            costs=args.costs,
            stops=args.stops,
        )
        print(
            f"runs {replayed.runs}, charts {replayed.charts}, proposals {replayed.proposals}, "
            f"events {replayed.events} ({replayed.events_disagreeing} disagreeing), "
            f"exits {replayed.exits}, stops {replayed.cells}"
        )
        return 0
    if args.command == "features":
        with factory() as session:
            points = {
                symbol: 10.0**-digits
                for symbol, digits in session.execute(
                    select(Instrument.symbol, Instrument.digits)
                ).tuples()
            }
        made = write_features(base, args.ohlcv, points)
        print(
            f"events {made.events}, charts {made.markets}, without bars {made.without_bars}"
            + (f" ({', '.join(made.missing_markets)})" if made.missing_markets else "")
        )
        return 0

    with factory() as session:
        report = export_sweep(session, args.sweep_id, args.out)
    print(
        f"runs {report.runs}, unmanaged {report.unmanaged_runs}, trades read {report.trades_read}, "
        f"events {report.events} ({report.events_disagreeing} disagreeing), "
        f"unreconciled {len(report.unreconciled)}"
    )
    return 1 if report.unreconciled else 0


if __name__ == "__main__":
    sys.exit(main())
