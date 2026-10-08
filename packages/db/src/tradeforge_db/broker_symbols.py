"""Reading and replacing the broker's symbol catalogue.

Two operations, and they are asymmetric on purpose. The host agent **replaces** the whole
snapshot; the API only **searches** it. That asymmetry is the ADR-02 boundary showing through
in the data layer: the process that can see MetaTrader is the only one allowed to say what the
broker offers, and the process that serves the screen never has to know MetaTrader exists.
"""

import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tradeforge_db.models import Broker, BrokerSymbol, Instrument

logger = logging.getLogger(__name__)

NAMED = 20
"""Skipped symbols a sync names in its log; the rest are counted."""

__all__ = [
    "MARKETS",
    "BrokerSymbolEntry",
    "BrowsedSymbol",
    "SymbolMatch",
    "browse_symbols",
    "market_of",
    "replace_snapshot",
    "search_symbols",
    "snapshot_taken_at",
    "symbol_path",
]

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
    """Make this server's part of the table say exactly this. Returns how many rows it wrote.

    ⚠️ **A replace per server, not an upsert, and not the whole table** (08/10). Upserting would
    keep every symbol the broker stopped offering, so a snapshot that cannot shrink would not be
    a snapshot. Replacing the *whole* table, as this did until 08/10, made the catalogue one
    broker's: syncing a second broker (US shares at Tradeview beside forex at ActivTrades) wiped
    the first one's list, and with it the market every collected instrument is classified by.
    Each server now replaces only its own rows; the others stand.

    ⚠️ **A symbol another server already owns is skipped, never taken over.** A name is one
    instrument here — the candles' folder, the instrument, the market a screen shows all key on
    it — and two brokers can mean two things by it: `GOLD` is the metal at ActivTrades and
    Barrick Gold's shares at Tradeview. The first broker to list it keeps it; the skip is
    logged. Telling them apart (the broker as part of an instrument's identity) is a larger
    change than a sync.

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

    session.execute(delete(BrokerSymbol).where(BrokerSymbol.server.is_not_distinct_from(server)))
    owned = set(
        session.scalars(
            select(BrokerSymbol.symbol).where(BrokerSymbol.server.is_distinct_from(server))
        )
    )
    skipped = sorted(entry.symbol for entry in entries if entry.symbol in owned)
    if skipped:
        logger.warning(
            "%d symbols of %s already belong to another server and were skipped: %s",
            len(skipped),
            server or "an unnamed server",
            ", ".join(skipped[:NAMED]) + (" ..." if len(skipped) > NAMED else ""),
        )
    kept = [entry for entry in entries if entry.symbol not in owned]
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
            for entry in kept
        ]
    )
    session.flush()
    return len(kept)


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
    broker: str | None = None
    """The slug of the broker whose terminal lists it (ADR-0032); `None` for a server nobody
    registered."""


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
        select(BrokerSymbol, Instrument.id, Broker.slug)
        .outerjoin(Instrument, Instrument.symbol == BrokerSymbol.symbol)
        .outerjoin(Broker, Broker.server == BrokerSymbol.server)
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
            broker=broker,
        )
        for row, instrument_id, broker in session.execute(statement)
    ]


def symbol_path(session: Session, symbol: str) -> str | None:
    """The tree path the snapshot photographed for this symbol, or `None`.

    ⚠️ **`None` covers two different situations and the caller has to keep them apart**: the
    symbol is not in the snapshot at all (nobody has synced, or the account cannot see it), or
    it is there with no path (MT5 returns the empty string for "not set", which
    `replace_snapshot` stores as NULL). Neither one lets anybody decide an asset class, which
    is what this is read for — so the collection screen asks in both cases rather than
    distinguishing them for no gain.
    """
    statement = select(BrokerSymbol.path).where(BrokerSymbol.symbol == symbol)
    return session.scalars(statement).one_or_none()


def snapshot_taken_at(session: Session) -> tuple[str | None, dt.datetime] | None:
    """The broker and the moment of the latest snapshot, or `None` if there is not one.

    Read off the most recent row: a replace writes one server's rows in one transaction with one
    timestamp, and with several servers the latest sync is the one worth naming.
    `None` is what tells the screen to say "never synced" rather than to show an empty list as
    though the broker offered nothing — the same distinction the replace refuses to blur.
    """
    row = session.scalars(
        select(BrokerSymbol).order_by(BrokerSymbol.synced_at.desc()).limit(1)
    ).first()
    return None if row is None else (row.server, row.synced_at)


MARKETS: tuple[tuple[str, str], ...] = (
    ("forex", "Forex"),
    ("crypto", "Crypto"),
    ("indices", "Indices"),
    ("metals", "Metals"),
    ("commodities", "Commodities"),
    ("stocks_us", "Stocks USA"),
    ("stocks_br", "Stocks BR"),
    ("stocks_other", "Stocks (other)"),
    ("futures", "Futures"),
    ("other", "Other"),
)
"""The markets the symbol browser groups a broker's list into, in the order its tabs show (02/10).

His ask: with hundreds of symbols, choose a market first — crypto, forex, indices, US and
Brazilian shares — then the symbols in it."""

_US_WORDS = frozenset({"us", "usa", "nasdaq", "nyse", "america", "american", "united"})
_BR_WORDS = frozenset({"br", "brazil", "brasil", "bovespa", "b3", "brazilian"})


_EXCHANGE_SUFFIX = re.compile(r"\.[a-z]{2,3}$")
"""A ticker's exchange suffix — `.FR`, `.SE`, `.US`, `.SA` — which marks a share wherever a broker
files it."""


def market_of(path: str | None, symbol: str) -> str:  # noqa: PLR0911 — one answer per market
    """The market a broker symbol belongs to, from its tree path and its name.

    ⚠️ **Read from words, not from one broker's folder names.** ActivTrades files currencies under
    `Forex`, indices under `Cash Indices`, shares under `CFD UK Shares`; another broker says
    `Stocks/US/AAPL` or names its tickers `PETR4.SA`. The order of the checks is the rule: a
    future on an index is a future, a crypto filed under `Crypto Currency` is not a currency.
    """
    text = (path or "").lower()
    words = set(text.replace("\\", " ").replace("/", " ").replace("_", " ").split())
    name = symbol.lower()
    if "crypto" in text:
        return "crypto"
    if "forward" in text or "future" in text or "bmf" in words:
        return "futures"
    if "metal" in text:
        return "metals"
    if "energ" in text or "commodit" in text or "oil" in words:
        return "commodities"
    if "indices" in text or "index" in text:
        return "indices"
    if "forex" in text or "currenc" in text or words & {"fx", "majors", "minors", "exotics"}:
        return "forex"
    if "bovespa" in words and "vista" in words:
        return "stocks_br"  # XP: `BOVESPA\A VISTA\PETR4`, the cash market
    if "share" in text or "stock" in text or "equit" in text or _EXCHANGE_SUFFIX.search(name):
        if name.endswith((".us", ".usa")) or words & _US_WORDS:
            return "stocks_us"
        if name.endswith(".sa") or words & _BR_WORDS:
            return "stocks_br"
        return "stocks_other"
    return "other"


@dataclass(frozen=True, slots=True)
class BrowsedSymbol:
    """A broker symbol as the browser lists it: a search hit, with its market."""

    match: SymbolMatch
    market: str
    spread_points: Decimal | None = None
    """The spread the collector measured when it catalogued the symbol; `None` until then."""


def browse_symbols(session: Session) -> list[BrowsedSymbol]:
    """Every symbol of the snapshot with its market, alphabetical — filtered and paged by the
    caller. A broker lists hundreds to a few thousand symbols: classified here once per request,
    in memory, because the market is read from words no column holds."""
    statement = (
        select(BrokerSymbol, Instrument.id, Instrument.default_spread_points, Broker.slug)
        .outerjoin(Instrument, Instrument.symbol == BrokerSymbol.symbol)
        .outerjoin(Broker, Broker.server == BrokerSymbol.server)
        .order_by(BrokerSymbol.symbol)
    )
    return [
        BrowsedSymbol(
            match=SymbolMatch(
                symbol=row.symbol,
                description=row.description,
                path=row.path,
                digits=row.digits,
                visible=row.visible,
                catalogued=instrument_id is not None,
                broker=broker,
            ),
            market=market_of(row.path, row.symbol),
            spread_points=spread,
        )
        for row, instrument_id, spread, broker in session.execute(statement)
    ]
