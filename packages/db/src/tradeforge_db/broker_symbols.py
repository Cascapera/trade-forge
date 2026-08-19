"""Reading and replacing the broker's symbol catalogue.

Two operations, and they are asymmetric on purpose. The host agent **replaces** the whole
snapshot; the API only **searches** it. That asymmetry is the ADR-02 boundary showing through
in the data layer: the process that can see MetaTrader is the only one allowed to say what the
broker offers, and the process that serves the screen never has to know MetaTrader exists.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tradeforge_db.models import BrokerSymbol, Instrument

__all__ = ["BrokerSymbolEntry", "SymbolMatch", "replace_snapshot", "search_symbols"]

# What one search will hand back at most. A combobox that renders 9550 rows is a combobox that
# renders none of them usefully, and the caller narrows by typing another letter — which is the
# affordance the limit is there to encourage rather than a defence against anything.
DEFAULT_LIMIT = 25


@dataclass(frozen=True, slots=True)
class BrokerSymbolEntry:
    """One symbol as the terminal describes it.

    A dataclass rather than the ORM row, so the collector can build these without a session
    open and without the writer and the reader sharing a mutable object. It is the same shape
    the `CatalogueEntry` next door takes for the same reason.
    """

    symbol: str
    description: str | None = None
    path: str | None = None
    digits: int | None = None
    visible: bool = False


def replace_snapshot(
    session: Session,
    entries: list[BrokerSymbolEntry],
    *,
    server: str | None,
    synced_at: dt.datetime,
) -> int:
    """Make the table say exactly this, and nothing else. Returns how many rows it now holds.

    ⚠️ **A replace, not an upsert, and the difference is the feature.** Upserting would leave
    behind every symbol the previous broker offered, so switching accounts would grow a list
    that is the union of every broker ever connected — and the screen would keep offering AAPL
    from an account that no longer has it. A snapshot that cannot shrink is not a snapshot.

    Deleting is safe here precisely because nothing references this table (see the model:
    `datasets` and `backtests` point at `instruments`, which this is deliberately not). If a
    foreign key is ever added, this function is the thing that breaks, and it should.

    ⚠️ **Refuses an empty snapshot.** A terminal that answers with no symbols at all is a
    terminal that is not logged in, or a `symbols_get` that failed — and neither of those is
    the statement "your broker offers nothing". Wiping the list on that reading would take the
    screen down whenever the sync ran at a bad moment, and the empty result is the *expected*
    failure, not a rare one.
    """
    if not entries:
        raise ValueError(
            "refusing to replace the symbol snapshot with an empty one: a terminal that "
            "lists no symbols is a terminal that is not logged in, not a broker with "
            "nothing to offer"
        )

    session.execute(delete(BrokerSymbol))
    session.add_all(
        [
            BrokerSymbol(
                symbol=entry.symbol,
                description=entry.description,
                path=entry.path,
                digits=entry.digits,
                visible=entry.visible,
                server=server,
                synced_at=synced_at,
            )
            for entry in entries
        ]
    )
    session.flush()
    return len(entries)


@dataclass(frozen=True, slots=True)
class SymbolMatch:
    """A search hit: what the broker offers, and whether this system can actually run it.

    ⚠️ **`catalogued` is the field that stops the screen from lying.** The snapshot holds every
    symbol the account can see — 84 on this broker — and exactly one of them has candles on
    disk. Offering all 84 identically would be offering 83 choices that fail on the button, and
    the user would learn which is which by clicking.

    It is a property of the *pair*, not of either table, which is why it is computed by the
    query rather than stored: a symbol becomes runnable the moment somebody collects it, and
    stops being so if its dataset is dropped.
    """

    symbol: str
    description: str | None
    path: str | None
    digits: int | None
    visible: bool
    catalogued: bool


def search_symbols(
    session: Session, prefix: str, *, limit: int = DEFAULT_LIMIT
) -> list[SymbolMatch]:
    """Symbols whose name starts with `prefix`, case-insensitively, alphabetical.

    A **prefix** match and not a substring one, because that is how a ticker is recalled: `aap`
    is somebody reaching for AAPL, and a substring match would bury it under every symbol with
    those letters in the middle. The trade-off is real and accepted — searching `usd` will not
    find EURUSD.

    ⚠️ Deliberately does **not** filter on `visible`. Measured on this project's broker: 74 of
    84 symbols are outside Market Watch, and every one of them answers a prefix query and hands
    over history exactly like a selected one. Filtering would hide seven eighths of the
    catalogue to enforce a distinction that only matters to the live loop.

    An empty prefix returns the first page rather than everything, which is what a combobox
    wants when it opens before anyone has typed.
    """
    # `startswith(..., autoescape=True)` so a symbol containing `%` or `_` — brokers do ship
    # names like `EURUSD.pro` and `US30_m` — is searched for literally instead of turning the
    # user's typing into a wildcard.
    # ⚠️ An OUTER join, and the direction matters: every broker symbol comes back, catalogued or
    # not. An inner join would silently narrow the search to what has already been collected,
    # which is precisely the list this whole feature exists to escape.
    #
    # Joined on `symbol` rather than on a foreign key, because there is none — see the model.
    # The overlap between the two tables is a coincidence of names, not a relation.
    statement = (
        select(BrokerSymbol, Instrument.id)
        .outerjoin(Instrument, Instrument.symbol == BrokerSymbol.symbol)
        .where(BrokerSymbol.symbol.istartswith(prefix, autoescape=True))
        .order_by(BrokerSymbol.symbol)
        .limit(limit)
    )
    return [
        SymbolMatch(
            symbol=row.symbol,
            description=row.description,
            path=row.path,
            digits=row.digits,
            visible=row.visible,
            catalogued=instrument_id is not None,
        )
        for row, instrument_id in session.execute(statement)
    ]


def snapshot_taken_at(session: Session) -> tuple[str | None, dt.datetime] | None:
    """The broker and the moment of the current snapshot, or `None` if there is not one.

    Read off any row, because a replace writes them all in one transaction with one timestamp.
    `None` is what tells the screen to say "never synced" rather than to show an empty list as
    though the broker offered nothing — the same distinction the replace refuses to blur.
    """
    row = session.scalars(select(BrokerSymbol).limit(1)).first()
    return None if row is None else (row.server, row.synced_at)
