"""`TradeRecorder` against a real Postgres: the three statements, and the CHECKs they meet.

`test_ledger_watch.py` proves what changed. This proves what that turns into on disk — and the
half that only exists with a database in it: the partial unique index that makes
`(live_session_id, entry_time)` a correlation key rather than a hope, and the
`exit_is_all_or_nothing` CHECK that decides what an open row is allowed to look like.
"""

import datetime as dt
from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.live.recorder import LedgerWatch, TradeRecorder, record_bar
from tradeforge_db.live_sessions import open_session
from tradeforge_db.models import ExitReason, Instrument, LiveSession, Strategy, Trade
from tradeforge_engine.domain import AssetClass, ClosedTrade, Position, Side

from .test_ledger_watch import SYMBOL, FakeBroker, a_position, a_trade

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


@pytest.fixture
def live(session: Session) -> LiveSession:
    instrument = Instrument(
        symbol=SYMBOL,
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    strategy = Strategy(
        definition={
            "schema_version": "1.0",
            "name": "MA Cross",
            "description": "an example",
            "timeframe": "H1",
        },
        version=1,
    )
    session.add_all([instrument, strategy])
    session.flush()
    row = open_session(
        session,
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "bar_spread"},
        engine_version="0.1.0",
        warmup_bars=62,
        at=NOON,
    )
    session.commit()
    return row


def rows_of(db: Session, live: LiveSession) -> list[Trade]:
    return list(
        db.execute(select(Trade).where(Trade.live_session_id == live.id).order_by(Trade.entry_time))
        .scalars()
        .all()
    )


def a_bar(db: Session, live: LiveSession, broker: FakeBroker, watch: LedgerWatch) -> None:
    record_bar(
        db,
        watch=watch,
        recorder=TradeRecorder(live.id, live.instrument_id),
        broker=broker,
    )
    db.commit()


def test_a_position_becomes_a_row_with_no_exit(session: Session, live: LiveSession) -> None:
    """⚠️ The whole reason the row is written at the fill. A session holding a position for
    three days must show something — writing only at the close makes it indistinguishable from
    a session that never traded (`specs/fase-3.md`).

    All four exit columns absent is what `exit_is_all_or_nothing` means by open, so this also
    proves the CHECK admits the state the design depends on.
    """
    broker = FakeBroker()
    broker.open = [a_position(0)]

    a_bar(session, live, broker, LedgerWatch(SYMBOL))

    (row,) = rows_of(session, live)
    assert row.entry_time == a_position(0).entry_time
    assert row.exit_time is None
    assert row.exit_price is None
    assert row.exit_reason is None
    assert row.net_pnl is None
    assert row.backtest_id is None, "a live trade must not claim a backtest parent"


def test_the_close_updates_the_row_rather_than_adding_one(
    session: Session, live: LiveSession
) -> None:
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    a_bar(session, live, broker, watch)

    broker.open = []
    broker.closed = [a_trade(0)]
    a_bar(session, live, broker, watch)

    (row,) = rows_of(session, live)
    assert row.exit_time == a_trade(0).exit_time
    assert row.exit_reason is ExitReason.TAKE_PROFIT
    assert row.net_pnl == Decimal("48.00000000")


def test_the_entry_is_settled_at_the_fill_and_the_close_cannot_move_it(
    session: Session, live: LiveSession
) -> None:
    """⚠️ A close that could rewrite the entry is a close that can rewrite history — and the R
    multiple the row reports would then have been computed against a stop the row no longer
    shows. Proved by handing the close a *different* entry price and volume and demanding the
    stored ones did not budge.
    """
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    a_bar(session, live, broker, watch)

    doctored = ClosedTrade(
        symbol=SYMBOL,
        side=Side.LONG,
        volume=Decimal("9.99"),
        entry_time=a_trade(0).entry_time,
        entry_price=Decimal("1.90000"),
        exit_time=a_trade(0).exit_time,
        exit_price=Decimal("1.10500"),
        gross_pnl=Decimal("50"),
        costs=Decimal("2"),
        net_pnl=Decimal("48"),
        reason="tp",
        stop_loss=Decimal("1.50000"),
    )
    broker.open = []
    broker.closed = [doctored]
    a_bar(session, live, broker, watch)

    (row,) = rows_of(session, live)
    assert row.entry_price == Decimal("1.10000000"), "the close rewrote the entry price"
    assert row.volume == Decimal("0.10"), "the close rewrote the size"
    assert row.stop_loss == Decimal("1.09500000"), "the close rewrote the risk it was sized on"


def test_the_row_records_the_stop_it_was_sized_against_not_the_trailed_one(
    session: Session, live: LiveSession
) -> None:
    """⚠️ `initial_stop_loss`, not `stop_loss`. A strategy that trails (ADR-0018) moves the
    live one while the trade runs, so writing that would make the recorded risk drift with the
    trailing — and every R multiple afterwards would divide by a denominator the trade never
    risked."""
    broker = FakeBroker()
    trailed = Position(
        symbol=SYMBOL,
        side=Side.LONG,
        volume=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        entry_time=NOON,
        initial_stop_loss=Decimal("1.09500"),
        stop_loss=Decimal("1.10200"),
    )
    broker.open = [trailed]

    a_bar(session, live, broker, LedgerWatch(SYMBOL))

    (row,) = rows_of(session, live)
    assert row.stop_loss == Decimal("1.09500000"), "the trailed stop was recorded as the risk"


def test_a_trade_that_opened_and_closed_inside_one_bar_is_one_finished_row(
    session: Session, live: LiveSession
) -> None:
    """No open row was ever written, so there is nothing to update — and an UPDATE would match
    zero rows and take the session down on its first scalp."""
    broker = FakeBroker()
    broker.closed = [a_trade(0)]

    a_bar(session, live, broker, LedgerWatch(SYMBOL))

    (row,) = rows_of(session, live)
    assert row.exit_time == a_trade(0).exit_time
    assert row.net_pnl == Decimal("48.00000000")


def test_a_stop_and_a_reversal_on_one_bar_leave_two_rows(
    session: Session, live: LiveSession
) -> None:
    """⚠️ The ordering inside `apply`, against the index that punishes getting it wrong. Both
    writes land in one transaction, and an open INSERT before the closing UPDATE would put two
    rows with the same key in flight."""
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)
    broker.open = [a_position(0)]
    a_bar(session, live, broker, watch)

    broker.open = [a_position(1)]
    broker.closed = [a_trade(0)]
    a_bar(session, live, broker, watch)

    closed_row, open_row = rows_of(session, live)
    assert closed_row.exit_time is not None
    assert open_row.exit_time is None
    assert open_row.entry_time == a_position(1).entry_time


def test_a_quiet_bar_writes_nothing(session: Session, live: LiveSession) -> None:
    broker = FakeBroker()
    watch = LedgerWatch(SYMBOL)

    for _ in range(5):
        a_bar(session, live, broker, watch)

    assert rows_of(session, live) == []


def test_a_close_that_finds_no_row_is_loud(session: Session, live: LiveSession) -> None:
    """⚠️ The alternative is a session that keeps trading while its record quietly stops
    matching. Reached by telling the recorder to close a trade whose open row was never written
    — the state a `merge` would have turned into a silent duplicate."""
    recorder = TradeRecorder(live.id, live.instrument_id)
    watch = LedgerWatch(SYMBOL)
    broker = FakeBroker()
    broker.open = [a_position(0)]
    watch.step(broker)

    broker.open = []
    broker.closed = [a_trade(0)]
    changes = watch.step(broker)

    with pytest.raises(RuntimeError, match="matched 0 rows"):
        recorder.apply(session, changes)


def test_two_sessions_entering_at_the_same_instant_do_not_collide(
    session: Session, live: LiveSession, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The correlation key is `(live_session_id, entry_time)`, not `entry_time`. Two paper
    sessions on the same symbol and timeframe enter on the same bar routinely — if the close
    matched on the instant alone it would update whichever row it found first, and the two
    sessions' records would swap trades."""
    other = open_session(
        session,
        strategy_id=live.strategy_id,
        instrument_id=live.instrument_id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "bar_spread"},
        engine_version="0.1.0",
        warmup_bars=62,
        at=NOON,
    )
    session.commit()

    # ⚠️ One watch per session, as in production: a session's bookkeeping is its own, and a
    # shared one would think the other session's row was its.
    brokers = {}
    watches = {}
    for owner in (live, other):
        brokers[owner.id] = FakeBroker()
        watches[owner.id] = LedgerWatch(SYMBOL)
        brokers[owner.id].open = [a_position(0)]
        a_bar(session, owner, brokers[owner.id], watches[owner.id])

    assert len(rows_of(session, live)) == 1
    assert len(rows_of(session, other)) == 1

    # Only `live` closes.
    brokers[live.id].open = []
    brokers[live.id].closed = [a_trade(0)]
    a_bar(session, live, brokers[live.id], watches[live.id])

    assert rows_of(session, live)[0].exit_time is not None
    assert rows_of(session, other)[0].exit_time is None, "the other session's row was closed"


def test_the_recorder_leaves_the_transaction_to_its_caller(
    session: Session, live: LiveSession, session_factory: Callable[[], Session]
) -> None:
    """⚠️ Not tidiness. The bar's other write — `last_bar_time` — has to land with these or not
    at all; a recorder that committed on its own would leave a window where a trade exists and
    the bar that produced it does not."""
    broker = FakeBroker()
    broker.open = [a_position(0)]

    record_bar(
        session,
        watch=LedgerWatch(SYMBOL),
        recorder=TradeRecorder(live.id, live.instrument_id),
        broker=broker,
    )

    with session_factory() as reader:
        assert rows_of(reader, live) == [], "the recorder committed on its own"

    session.commit()
    assert len(rows_of(session, live)) == 1
