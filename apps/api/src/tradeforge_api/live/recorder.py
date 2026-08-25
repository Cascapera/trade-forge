"""What a live session leaves behind, one bar at a time.

A backtest reports at the end, because it has one. A paper session's end is whenever somebody
stops it, so anything it has not written down by then is lost — and a session holding a position
for three days has something true to say with no ending in which to say it. So the row is
written at the fill, with the four exit columns NULL, and **updated** when the trade closes
(`specs/fase-3.md`, 24/08).

The decision — *what changed in the ledger since the last bar* — is separated from the writing,
and deliberately so. It is where the bugs are (a trade that opens and closes inside one bar; a
close that must find its own open row rather than insert a second one), and `LedgerWatch` needs
no database to answer it. `TradeRecorder` is the part that cannot be tested in milliseconds, and
it is a translation of the answer into three statements.

That split is the same one `is_stale` records: a rule reachable only through Postgres is a rule
the coverage gate does not see (`ci.yml` runs plain `pytest` with a 90% floor), and a test that
cannot call the decision ends up restating it.
"""

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.orm import Session

from tradeforge_db.models import Trade
from tradeforge_db.results import close_trade_values, closed_trade_row, open_trade_row
from tradeforge_engine.domain import ClosedTrade, Position

__all__ = ["BarChanges", "LedgerView", "LedgerWatch", "TradeRecorder", "record_bar"]


class LedgerView(Protocol):
    """The two questions this module asks a broker, and no more.

    Narrower than `Broker` on purpose. Nothing here submits, cancels or steps a bar, and a
    parameter typed `Broker` states a requirement that does not exist. That is not a
    stylistic point: it showed up immediately as tests unable to ask "what are you holding"
    without first building a whole venue, and a fake that large is a fake that drifts.

    `BacktestBroker` satisfies it structurally, and `test_ledger_watch.py` proves that by
    assignment rather than by claiming it here.
    """

    def positions(self, symbol: str) -> Sequence[Position]: ...

    def trades(self) -> Sequence[ClosedTrade]: ...


@dataclass(frozen=True, slots=True)
class BarChanges:
    """The writes one bar earned. Empty on the overwhelming majority of bars."""

    to_close: tuple[ClosedTrade, ...] = ()
    """Round trips whose open row is already on disk. An UPDATE, found by `(session, entry)`."""

    to_insert_closed: tuple[ClosedTrade, ...] = ()
    """Round trips that opened and closed **inside this bar**, so no open row was ever written.

    ⚠️ Not the same as `to_close` with an INSERT instead of an UPDATE. There was no moment at
    which anybody could have seen this trade open, so writing an open row and updating it in the
    same transaction would record a state the market never had.
    """

    to_open: Position | None = None
    """A position that appeared this bar. Its row carries no exit yet."""

    def __bool__(self) -> bool:
        return bool(self.to_close or self.to_insert_closed or self.to_open is not None)


@dataclass(slots=True)
class LedgerWatch:
    """The broker's ledger, differenced bar by bar. Pure, and the whole decision lives here.

    Holds two pieces of state, and both are needed for the same reason: the broker reports
    *what is true now*, never *what changed*. How many round trips it had already closed, and
    which entry instant already has a row on disk.
    """

    symbol: str
    _closed_seen: int = 0
    _row_written_for: set[dt.datetime] = field(default_factory=set)

    def step(self, broker: LedgerView) -> BarChanges:
        """What this bar changed. Call once per `BarOutcome`, in order.

        ⚠️ **Closes are worked out before opens.** A single bar can end one trade and begin
        another — a stop hit and a reversal entered on the same candle — and reading the open
        position first would attribute the new position's entry to the row the closed trade is
        about to claim. The two are keyed on `entry_time`, so getting the order wrong is not a
        crash: it is one trade recorded with another's entry.
        """
        settled = broker.trades()
        fresh = tuple(settled[self._closed_seen :])
        self._closed_seen = len(settled)

        to_close: list[ClosedTrade] = []
        to_insert: list[ClosedTrade] = []
        for trade in fresh:
            if trade.entry_time in self._row_written_for:
                self._row_written_for.discard(trade.entry_time)
                to_close.append(trade)
            else:
                to_insert.append(trade)

        return BarChanges(
            to_close=tuple(to_close),
            to_insert_closed=tuple(to_insert),
            to_open=self._unrecorded_position(broker),
        )

    def _unrecorded_position(self, broker: LedgerView) -> Position | None:
        """The open position this session has not written a row for yet, if there is one.

        ⚠️ Asked of the broker every bar rather than tracked from fills. A fill is an event and
        events can be missed; `positions()` is the venue's own answer to "what am I holding",
        which is the same question the row is trying to record. It is also what an `MT5Broker`
        will answer from a real account.
        """
        for position in broker.positions(self.symbol):
            if position.entry_time not in self._row_written_for:
                self._row_written_for.add(position.entry_time)
                return position
        return None


@dataclass(slots=True)
class TradeRecorder:
    """`BarChanges` as rows. Adds to the caller's `Session`; never commits.

    The transaction is the session process's to own, because the bar's other write —
    `last_bar_time` — has to land with these or not at all. A recorder that committed on its own
    would leave a window where a trade exists and the bar that produced it does not.
    """

    session_id: uuid.UUID
    instrument_id: uuid.UUID

    def apply(self, db: Session, changes: BarChanges) -> None:
        # Closes first, mirroring `LedgerWatch.step` — and load-bearing for the same reason. An
        # INSERT of a new position's open row before the UPDATE that finishes the previous trade
        # would put two rows with the same `(live_session_id, entry_time)` in flight whenever a
        # reversal reuses an instant, and the partial unique index would refuse the second.
        for trade in changes.to_close:
            self._close(db, trade)
        for trade in changes.to_insert_closed:
            db.add(closed_trade_row(trade, self.session_id, self.instrument_id))
        if changes.to_open is not None:
            db.add(open_trade_row(changes.to_open, self.session_id, self.instrument_id))

    def _close(self, db: Session, trade: ClosedTrade) -> None:
        """Fill in the exit half of a row that is already there.

        ⚠️ An UPDATE rather than a `merge`. The correlation is `(live_session_id, entry_time)`,
        and a merge that failed to match would **insert** — turning "the close could not find its
        trade" into a duplicate row nobody notices, instead of a count this can check.
        """
        # ⚠️ `execute` is typed as returning `Result`, which has no `rowcount`; an UPDATE really
        # returns a `CursorResult`, and the count is the only thing this check has to work with.
        result = cast(
            "CursorResult[Any]",
            db.execute(
                update(Trade)
                .where(
                    Trade.live_session_id == self.session_id,
                    Trade.entry_time == trade.entry_time,
                )
                .values(**close_trade_values(trade))
            ),
        )
        if result.rowcount != 1:
            # Loud, because the alternative is a session that keeps trading while its record
            # quietly stops matching. `rowcount` of 0 means the open row is missing; more than
            # one is impossible while the partial unique index stands, and worth saying so.
            raise RuntimeError(
                f"closing the trade entered at {trade.entry_time.isoformat()} matched "
                f"{result.rowcount} rows, expected exactly 1"
            )


def record_bar(
    db: Session, *, watch: LedgerWatch, recorder: TradeRecorder, broker: LedgerView
) -> BarChanges:
    """One bar's bookkeeping: look, then write. Returns what it wrote, for the caller to log."""
    changes = watch.step(broker)
    if changes:
        recorder.apply(db, changes)
    return changes
