"""`tradeforge-db` — bring a database to head, or fill it with example data.

    tradeforge-db upgrade            # apply every migration
    tradeforge-db downgrade 0001     # roll back to a revision (`base` = empty)
    tradeforge-db seed               # example instruments, safe to re-run

The connection always comes from the environment. There is no `--dsn` flag on
purpose: a database URL typed on a command line ends up in the shell history, in
`ps`, and eventually in a screenshot.
"""

import argparse
from collections.abc import Sequence

from tradeforge_db.migrate import downgrade, upgrade
from tradeforge_db.seeds import seed_instruments
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tradeforge-db", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    up = commands.add_parser("upgrade", help="apply migrations forward")
    up.add_argument("revision", nargs="?", default="head")

    # No default revision. `downgrade` with no argument would be a foot-gun that
    # quietly drops a table because someone hit enter early.
    down = commands.add_parser("downgrade", help="roll migrations back")
    down.add_argument("revision")

    commands.add_parser("seed", help="insert the example instruments (idempotent)")

    return parser


def _seed() -> int:
    engine = create_db_engine()
    try:
        with session_scope(create_session_factory(engine)) as session:
            written = seed_instruments(session)
    finally:
        engine.dispose()

    print(f"seeded {written} instruments")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a subcommand and return a shell exit code."""
    args = _parser().parse_args(argv)

    if args.command == "upgrade":
        upgrade(args.revision)
        print(f"upgraded to {args.revision}")
        return 0

    if args.command == "downgrade":
        downgrade(args.revision)
        print(f"downgraded to {args.revision}")
        return 0

    return _seed()
