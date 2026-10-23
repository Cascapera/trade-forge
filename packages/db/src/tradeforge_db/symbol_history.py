"""Storing and reading what a probe found about one (symbol, timeframe).

An upsert and a read, and the asymmetry is the ADR-02 boundary again: the host agent is the only
process that can ask MetaTrader anything, so it is the only one that writes; the API serves the
screen and never learns MetaTrader exists.
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradeforge_db.models import SymbolHistory

__all__ = ["HistoryProbe", "read_history", "upsert_history"]


@dataclass(frozen=True, slots=True)
class HistoryProbe:
    """One probe's findings, in the shape the database stores.

    A dataclass rather than the ORM row so the agent can build it without a session open — the
    same seam `BrokerSymbolEntry` and `CatalogueEntry` use next door, for the same reason.
    """

    symbol: str
    timeframe: str
    oldest: dt.datetime | None
    bar_count: int
    terminal_maxbars: int
    bar_count_is_a_ceiling: bool
    last_fabricated: int | None
    first_measured_cost: int | None


def upsert_history(session: Session, probe: HistoryProbe, *, probed_at: dt.datetime) -> None:
    """Record what the probe found, replacing any earlier answer for the same series.

    ⚠️ **An upsert, not an append, and unlike the symbol snapshot next door this one is keyed.**
    A probe is a question about one series and its answer changes over time — history gets
    downloaded, `maxbars` gets raised, a broker starts measuring its spread. Appending would
    leave the screen to pick between contradictory accounts of the same series by date, and
    picking the wrong one means sizing a window from a stale ceiling.

    `probed_at` is passed in rather than read from the clock here, so the caller that did the
    measuring is the one that says when — and so a test can assert the field at all.
    """
    statement = insert(SymbolHistory).values(
        symbol=probe.symbol,
        timeframe=probe.timeframe,
        oldest=probe.oldest,
        bar_count=probe.bar_count,
        terminal_maxbars=probe.terminal_maxbars,
        bar_count_is_a_ceiling=probe.bar_count_is_a_ceiling,
        last_fabricated=probe.last_fabricated,
        first_measured_cost=probe.first_measured_cost,
        probed_at=probed_at,
    )
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[SymbolHistory.symbol, SymbolHistory.timeframe],
            set_={
                "oldest": statement.excluded.oldest,
                "bar_count": statement.excluded.bar_count,
                "terminal_maxbars": statement.excluded.terminal_maxbars,
                "bar_count_is_a_ceiling": statement.excluded.bar_count_is_a_ceiling,
                "last_fabricated": statement.excluded.last_fabricated,
                "first_measured_cost": statement.excluded.first_measured_cost,
                "probed_at": statement.excluded.probed_at,
            },
        )
    )


def read_history(session: Session, symbol: str, timeframe: str) -> SymbolHistory | None:
    """What is known about this series, or `None` if it has never been probed.

    ⚠️ `None` is a real answer the screen has to render differently from a probe that found
    nothing: "nobody has asked yet" invites a click, "this symbol has no bars" does not.
    """
    statement = select(SymbolHistory).where(
        SymbolHistory.symbol == symbol, SymbolHistory.timeframe == timeframe
    )
    return session.scalars(statement).one_or_none()
