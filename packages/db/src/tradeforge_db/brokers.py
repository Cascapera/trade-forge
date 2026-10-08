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

from tradeforge_db.models import Broker, Instrument

__all__ = ["BrokerChangedError", "UnknownBrokerError", "broker_for_server", "refuse_another_broker"]


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
