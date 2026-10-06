"""`tradeforge-ml` — the meta-labeling tools (ADR-0031).

    tradeforge-ml export <sweep-id> [--out data/ml]   # the sweep's event base, in Parquet
    tradeforge-ml features <sweep-id> [--out data/ml] [--ohlcv data/ohlcv]
                                                      # its variables, beside the events
    tradeforge-ml train <sweep-id> [--out data/ml] [--validation 2023-01-01:2025-01-01]
                       [--exclude 2020-03-01:2020-07-01 ...]
                                                      # the first meta-label, judged; files only
    tradeforge-ml replay <sweep-id> [--out data/ml] [--ohlcv data/ohlcv] [--horizon 150]
                         [--processes N]              # every proposal, traded alone
                                                      #   -> sweep=<id>/independent-h150/

`features` and `train` read the run-based base; `--base independent-h150` points them at the
replayed one instead.

Read-only on the database. Connects with the same `POSTGRES_*` settings as `tradeforge-db`;
`features` reads only each instrument's point there, to put its spread in price.
"""

import argparse
import datetime as dt
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from tradeforge_db.models import Instrument
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_ml.export import export_sweep
from tradeforge_ml.feature_export import write_features
from tradeforge_ml.replay import HORIZON
from tradeforge_ml.replay_export import jobs_of, write_independent


def span(text: str) -> tuple[dt.datetime, dt.datetime]:
    """`2023-01-01:2025-01-01` — from the first day, up to but not including the second."""
    left, right = (
        dt.datetime.fromisoformat(part).replace(tzinfo=dt.UTC) for part in text.split(":")
    )
    if right <= left:
        raise argparse.ArgumentTypeError(f"{text}: the end must come after the start")
    return left, right


def main(argv: Sequence[str] | None = None) -> int:
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
    again = commands.add_parser("replay", help="replay every proposal of the unmanaged runs")
    again.add_argument("sweep_id", type=uuid.UUID)
    again.add_argument("--out", type=Path, default=Path("data/ml"))
    again.add_argument("--ohlcv", type=Path, default=Path("data/ohlcv"))
    again.add_argument("--horizon", type=int, default=HORIZON)
    again.add_argument("--processes", type=int, default=None)
    args = parser.parse_args(argv)
    base = args.out / f"sweep={args.sweep_id}" / getattr(args, "base", "")

    if args.command == "train":
        # Here, not at the top: the models are an extra (`tradeforge-ml[models]`), absent from the
        # images, and every other command must run without them.
        from tradeforge_ml.model import Split  # noqa: PLC0415
        from tradeforge_ml.train import train  # noqa: PLC0415

        start, end = args.validation
        name, judged = train(base, Split(start, end, tuple(args.exclude)))
        everything, half = judged.validation[0], judged.validation[2]
        print(
            f"{name}: validation AUC {judged.validation_auc:.3f}, "
            f"mean R all {everything.mean_r:+.3f}, best half {half.mean_r:+.3f}"
        )
        return 0

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
        )
        print(
            f"runs {replayed.runs}, charts {replayed.charts}, proposals {replayed.proposals}, "
            f"events {replayed.events} ({replayed.events_disagreeing} disagreeing), "
            f"exits {replayed.exits}"
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
