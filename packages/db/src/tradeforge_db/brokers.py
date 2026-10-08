"""The brokers this system collects from (ADR-0032), and the rule that an instrument keeps its own.

A broker is known by its MT5 server — what a logged-in terminal reports — so the collector can say
which broker it is standing next to without being told. The brokers themselves are few and written
on purpose (a migration, a screen): a terminal logged into a server nobody registered is refused,
not registered on the fly, because its clock would be a guess.
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import Broker, BrokerSymbol, Instrument

__all__ = [
    "LEGACY_QUEUE",
    "BrokerChangedError",
    "UnknownBrokerError",
    "broker_by_slug",
    "broker_for_server",
    "broker_slug_for_symbol",
    "broker_slugs",
    "broker_ticker",
    "collect_queue",
    "keeps_path",
    "refuse_another_broker",
]

LEGACY_QUEUE = "collect"
"""The one queue there was before several brokers (ADR-0021): what an agent started without a
broker drains, and where a symbol no broker claims is still sent."""


def collect_queue(slug: str | None) -> str:
    """The queue one broker's agent drains (ADR-0032): `collect.<slug>`, or the legacy one."""
    return LEGACY_QUEUE if slug is None else f"{LEGACY_QUEUE}.{slug}"


class UnknownBrokerError(LookupError):
    """A terminal is logged into a server no broker row names — or whose clock disagrees."""

    def __init__(self, server: str | None, message: str | None = None) -> None:
        self.server = server
        super().__init__(
            message
            or (
                f"no broker is registered for the MT5 server {server!r}: register it (its clock "
                "included) before collecting from it — see ADR-0032"
                if server
                else "the terminal is not logged in: no MT5 server to tell the broker by"
            )
        )

    @classmethod
    def wrong_clock(
        cls, slug: str, recorded: dt.timedelta, stated: dt.timedelta
    ) -> "UnknownBrokerError":
        """The agent was started with another broker's clock than the terminal it found open."""
        hours = dt.timedelta(hours=1)
        return cls(
            slug,
            f"the open terminal is {slug}'s, whose clock is {recorded / hours:+g} h, but the agent "
            f"was started with {stated / hours:+g} h: restart it with {slug}'s clock",
        )


class BrokerChangedError(ValueError):
    """An instrument would move to another broker under the same internal name."""

    def __init__(self, symbol: str, recorded: str, offered: str) -> None:
        self.symbol = symbol
        self.recorded = recorded
        self.offered = offered
        super().__init__(
            f"{symbol} comes from {recorded}, not {offered}: two brokers' tickers are two "
            "instruments here, under two internal names (ADR-0032)"
        )


def keeps_path(catalogue_paths: list[str] | None, path: str | None) -> bool:
    """Whether a sync keeps a symbol filed under `path`: no list keeps everything; otherwise the
    path must be one of the folders or inside one, ignoring case and the slash's direction."""
    if catalogue_paths is None:
        return True
    folder = (path or "").replace("/", "\\").lower()
    return any(
        folder == root or folder.startswith(root + "\\")
        for root in (one.replace("/", "\\").rstrip("\\").lower() for one in catalogue_paths)
    )


def broker_for_server(session: Session, server: str | None) -> Broker:
    """The broker whose MT5 server this is, or `UnknownBrokerError`."""
    found = (
        None
        if server is None
        else session.scalars(select(Broker).where(Broker.server == server)).one_or_none()
    )
    if found is None:
        raise UnknownBrokerError(server)
    return found


def refuse_another_broker(session: Session, symbol: str, broker_id: uuid.UUID) -> None:
    """Raise `BrokerChangedError` if `symbol` is already another broker's instrument.

    ⚠️ The internal name is the key of the candles' folder, the sweeps and the screens: letting a
    second broker write under it would put two markets' bars in one series (`GOLD` the metal and
    `GOLD` Barrick Gold). A row with no broker yet — a seed — may be claimed.
    """
    recorded = session.execute(
        select(Broker.slug)
        .join(Instrument, Instrument.broker_id == Broker.id)
        .where(Instrument.symbol == symbol, Broker.id != broker_id)
    ).scalar_one_or_none()
    if recorded is not None:
        offered = session.scalars(select(Broker.slug).where(Broker.id == broker_id)).one()
        raise BrokerChangedError(symbol, recorded, offered)


def broker_by_slug(session: Session, slug: str) -> Broker:
    """The broker named `slug`, or `LookupError` naming the ones there are."""
    found = session.scalars(select(Broker).where(Broker.slug == slug)).one_or_none()
    if found is None:
        known = ", ".join(broker_slugs(session)) or "none"
        raise LookupError(f"no broker {slug!r}; registered: {known}")
    return found


def broker_slugs(session: Session) -> list[str]:
    """Every registered broker's slug, in order."""
    return list(session.scalars(select(Broker.slug).order_by(Broker.slug)))


def broker_ticker(session: Session, symbol: str) -> str:
    """What to ask the terminal for to collect `symbol`: its instrument's own ticker when it has
    one that differs (`WIN` → `WIN$`, ADR-0032), else the name itself."""
    ticker = session.scalars(
        select(Instrument.broker_symbol).where(Instrument.symbol == symbol)
    ).one_or_none()
    return ticker or symbol


def broker_slug_for_symbol(session: Session, symbol: str, ticker: str | None = None) -> str | None:
    """The broker a symbol is collected from: its instrument's, or — for a symbol not catalogued
    yet — the broker whose terminal lists it, or lists `ticker` when the internal name differs
    (`broker_symbols.server`). `None` when neither says.
    """
    owner = session.scalars(
        select(Broker.slug)
        .join(Instrument, Instrument.broker_id == Broker.id)
        .where(Instrument.symbol == symbol)
    ).one_or_none()
    if owner is not None:
        return owner
    return session.scalars(
        select(Broker.slug)
        .join(BrokerSymbol, BrokerSymbol.server == Broker.server)
        .where(BrokerSymbol.symbol == (ticker or symbol))
    ).first()
