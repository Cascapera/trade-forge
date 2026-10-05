"""`tradeforge-ml` — the meta-labeling tools (ADR-0031).

    tradeforge-ml export <sweep-id> [--out data/ml]   # the sweep's event base, in Parquet
    tradeforge-ml features <sweep-id> [--out data/ml] [--ohlcv data/ohlcv]
                                                      # its variables, beside the events

Read-only on the database. Connects with the same `POSTGRES_*` settings as `tradeforge-db`.
"""

import argparse
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_ml.export import export_sweep
from tradeforge_ml.feature_export import write_features


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
    args = parser.parse_args(argv)

    if args.command == "features":
        made = write_features(args.out / f"sweep={args.sweep_id}", args.ohlcv)
        print(
            f"events {made.events}, charts {made.markets}, without bars {made.without_bars}"
            + (f" ({', '.join(made.missing_markets)})" if made.missing_markets else "")
        )
        return 0

    factory = create_session_factory(create_db_engine())
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
