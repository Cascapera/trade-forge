"""Live setups: what the signals follow, on which markets, and what they have posted (09/10).

A setup is one strategy version on one timeframe, with its costs and the R a target-less signal
closes at; its markets are added, switched off or dropped at any time. The signals it posted are
kept one row per number (`SignalRecord`), and its metrics — in R only, his call — come from them.
"""

import datetime as dt
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    Broker,
    Instrument,
    LiveSetup,
    LiveSetupMarket,
    SignalRecord,
    Strategy,
)

__all__ = [
    "AlreadyFollowedError",
    "FollowedPair",
    "OpenSignal",
    "SetupMetrics",
    "SetupView",
    "add_market",
    "create_setup",
    "create_with_strategy",
    "delete_setup",
    "follow_backtest",
    "followed_pairs",
    "list_setups",
    "new_version",
    "open_signal",
    "orphan_signals",
    "record_event",
    "remove_market",
    "set_market",
    "setup_metrics",
    "signals_of",
    "update_setup",
]


class AlreadyFollowedError(ValueError):
    """The same strategy on the same market and timeframe is already followed — every signal
    would be posted twice."""


@dataclass(frozen=True, slots=True)
class MarketView:
    instrument_id: uuid.UUID
    symbol: str
    broker: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class SetupView:
    """A setup with the names a screen shows beside it."""

    setup: LiveSetup
    strategy_name: str
    markets: list[MarketView]


@dataclass(frozen=True, slots=True)
class FollowedPair:
    """One session the supervisor keeps running: a setup on one market."""

    setup_id: uuid.UUID
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    cost_model: Mapping[str, object]
    no_target_r: Decimal
    broker: str | None


# --------------------------------------------------------------------------- setups


def _refuse_double(
    session: Session,
    strategy_id: uuid.UUID,
    timeframe: str,
    instrument_id: uuid.UUID,
    *,
    besides: uuid.UUID | None = None,
) -> None:
    statement = (
        select(LiveSetup.name)
        .join(LiveSetupMarket, LiveSetupMarket.setup_id == LiveSetup.id)
        .where(
            LiveSetup.strategy_id == strategy_id,
            LiveSetup.timeframe == timeframe,
            LiveSetup.active,
            LiveSetupMarket.instrument_id == instrument_id,
            LiveSetupMarket.active,
        )
    )
    if besides is not None:
        statement = statement.where(LiveSetup.id != besides)
    clash = session.scalars(statement).first()
    if clash is not None:
        raise AlreadyFollowedError(f"already followed on this market by the setup {clash!r}")


def create_setup(  # noqa: PLR0913 — what a setup is, each its own field
    session: Session,
    *,
    strategy_id: uuid.UUID,
    timeframe: str,
    cost_model: Mapping[str, object],
    instrument_ids: Sequence[uuid.UUID],
    name: str | None = None,
    no_target_r: Decimal = Decimal(5),
    source_backtest_id: uuid.UUID | None = None,
) -> LiveSetup:
    """A new setup following these markets. `LookupError` for an unknown strategy."""
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise LookupError(f"no strategy with id {strategy_id}")
    for instrument_id in instrument_ids:
        _refuse_double(session, strategy_id, timeframe, instrument_id)
    setup = LiveSetup(
        name=name or strategy.name,
        strategy_id=strategy_id,
        timeframe=timeframe,
        cost_model=dict(cost_model),
        no_target_r=no_target_r,
        source_backtest_id=source_backtest_id,
        markets=[LiveSetupMarket(instrument_id=one) for one in dict.fromkeys(instrument_ids)],
    )
    session.add(setup)
    session.flush()
    return setup


def create_with_strategy(  # noqa: PLR0913 — what a setup registered from scratch is
    session: Session,
    *,
    definition: Mapping[str, object],
    timeframe: str,
    instrument_ids: Sequence[uuid.UUID],
    cost_model: Mapping[str, object],
    no_target_r: Decimal = Decimal(5),
) -> LiveSetup:
    """A setup registered from scratch (09/10): its strategy saved, and the setup made, in one
    transaction — a strategy without its setup is never left behind by a refusal.

    The caller validates the document. A name used before becomes that strategy's next version
    rather than a refused duplicate."""
    document = dict(definition)
    name = str(document.get("name") or "live setup")
    # A lineage starts at version 1 (the table's own check): a name used before continues its
    # newest version as the parent.
    latest = session.scalars(
        select(Strategy).where(Strategy.name == name).order_by(Strategy.version.desc())
    ).first()
    strategy = Strategy(
        definition=document,
        version=1 if latest is None else latest.version + 1,
        parent_version_id=None if latest is None else latest.id,
    )
    session.add(strategy)
    session.flush()
    return create_setup(
        session,
        strategy_id=strategy.id,
        timeframe=timeframe,
        cost_model=cost_model,
        instrument_ids=instrument_ids,
        name=name,
        no_target_r=no_target_r,
    )


def follow_backtest(
    session: Session, backtest_id: uuid.UUID, *, no_target_r: Decimal = Decimal(5)
) -> LiveSetup:
    """Follow what a finished run was. A setup already following this strategy on this timeframe
    gains the run's market instead of a second setup being made — his "include other markets"."""
    run = session.get(Backtest, backtest_id)
    if run is None:
        raise LookupError(f"no run with id {backtest_id}")
    if run.status is not BacktestStatus.DONE:
        raise LookupError(f"run {backtest_id} is {run.status.value}, not done: nothing to follow")
    existing = session.scalars(
        select(LiveSetup)
        .where(LiveSetup.strategy_id == run.strategy_id, LiveSetup.timeframe == run.timeframe)
        .order_by(LiveSetup.active.desc(), LiveSetup.created_at)
    ).first()
    if existing is None:
        return create_setup(
            session,
            strategy_id=run.strategy_id,
            timeframe=run.timeframe,
            cost_model=run.cost_model,
            instrument_ids=[run.instrument_id],
            no_target_r=no_target_r,
            source_backtest_id=run.id,
        )
    add_market(session, existing.id, run.instrument_id)
    return existing


def _setup(session: Session, setup_id: uuid.UUID) -> LiveSetup:
    setup = session.get(LiveSetup, setup_id)
    if setup is None:
        raise LookupError(f"no live setup with id {setup_id}")
    return setup


def add_market(session: Session, setup_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    """Follow one more market — or switch it back on if the setup had it."""
    setup = _setup(session, setup_id)
    if session.get(Instrument, instrument_id) is None:
        raise LookupError(f"no instrument with id {instrument_id}")
    _refuse_double(session, setup.strategy_id, setup.timeframe, instrument_id, besides=setup.id)
    market = next((one for one in setup.markets if one.instrument_id == instrument_id), None)
    if market is None:
        setup.markets.append(LiveSetupMarket(instrument_id=instrument_id))
    else:
        market.active = True
    session.flush()


def set_market(
    session: Session, setup_id: uuid.UUID, instrument_id: uuid.UUID, *, active: bool
) -> None:
    """Switch one market on or off; the setup keeps it either way."""
    setup = _setup(session, setup_id)
    market = next((one for one in setup.markets if one.instrument_id == instrument_id), None)
    if market is None:
        raise LookupError("the setup does not follow that market")
    if active:
        _refuse_double(session, setup.strategy_id, setup.timeframe, instrument_id, besides=setup.id)
    market.active = active
    session.flush()


def remove_market(session: Session, setup_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    """Drop a market from the setup. Its signals stay in the history."""
    setup = _setup(session, setup_id)
    market = next((one for one in setup.markets if one.instrument_id == instrument_id), None)
    if market is None:
        raise LookupError("the setup does not follow that market")
    setup.markets.remove(market)
    session.flush()


def update_setup(  # noqa: PLR0913 — keyword-only; each one thing a setup can change
    session: Session,
    setup_id: uuid.UUID,
    *,
    name: str | None = None,
    active: bool | None = None,
    no_target_r: Decimal | None = None,
    note: str | None = None,
) -> LiveSetup:
    """Change what a setup is called, whether it runs, its no-target R or its note."""
    setup = _setup(session, setup_id)
    if active:
        for market in setup.markets:
            if market.active:
                _refuse_double(
                    session,
                    setup.strategy_id,
                    setup.timeframe,
                    market.instrument_id,
                    besides=setup.id,
                )
    if name is not None:
        setup.name = name
    if active is not None:
        setup.active = active
    if no_target_r is not None:
        setup.no_target_r = no_target_r
    if note is not None:
        setup.note = note or None
    session.flush()
    return setup


def new_version(
    session: Session, setup_id: uuid.UUID, definition: Mapping[str, object]
) -> LiveSetup:
    """Edit the setup (09/10): a new version of its strategy, and the setup pointed at it.

    ⚠️ **Never the strategy in place** (AGENTS §5.5): the signals already posted keep the version
    they came from, so a history can be read before and after an edit. The version number is the
    next free one for that name, not merely parent + 1 — a strategy edited twice from the same
    parent would otherwise collide with itself.

    The caller validates the document; this only records it. Every market of the setup switches
    to the new version at once: the supervisor sees a different strategy and restarts its sessions.
    """
    setup = _setup(session, setup_id)
    parent = session.get(Strategy, setup.strategy_id)
    if parent is None:
        raise LookupError(f"the strategy behind setup {setup_id} is gone")
    document = dict(definition)
    name = str(document.get("name") or parent.name)
    latest = session.scalars(
        select(Strategy.version).where(Strategy.name == name).order_by(Strategy.version.desc())
    ).first()
    successor = Strategy(
        definition=document,
        version=(latest or parent.version) + 1,
        parent_version_id=parent.id,
    )
    session.add(successor)
    session.flush()
    setup.strategy_id = successor.id
    session.flush()
    return setup


def delete_setup(session: Session, setup_id: uuid.UUID) -> None:
    """Stop following it for good. Its signals stay in the history, without a setup."""
    session.delete(_setup(session, setup_id))
    session.flush()


def list_setups(session: Session) -> list[SetupView]:
    """Every setup, running first, newest first, with its markets."""
    setups = session.scalars(
        select(LiveSetup)
        .options(selectinload(LiveSetup.markets))
        .order_by(LiveSetup.active.desc(), LiveSetup.created_at.desc(), LiveSetup.id)
    ).all()
    names = {
        row.id: row.name
        for row in session.execute(
            select(Strategy.id, Strategy.name).where(
                Strategy.id.in_(list({one.strategy_id for one in setups}))
            )
        )
    }
    instruments = {
        row.id: (row.symbol, row.slug)
        for row in session.execute(
            select(Instrument.id, Instrument.symbol, Broker.slug)
            .outerjoin(Broker, Broker.id == Instrument.broker_id)
            .where(
                Instrument.id.in_(list({m.instrument_id for one in setups for m in one.markets}))
            )
        )
    }
    return [
        SetupView(
            setup=one,
            strategy_name=names.get(one.strategy_id, ""),
            markets=[
                MarketView(
                    instrument_id=market.instrument_id,
                    symbol=instruments[market.instrument_id][0],
                    broker=instruments[market.instrument_id][1],
                    active=market.active,
                )
                for market in one.markets
            ],
        )
        for one in setups
    ]


def followed_pairs(session: Session) -> list[FollowedPair]:
    """Every (setup, market) both switched on — one SIGNAL session each."""
    statement = (
        select(LiveSetup, LiveSetupMarket.instrument_id, Broker.slug)
        .join(LiveSetupMarket, LiveSetupMarket.setup_id == LiveSetup.id)
        .join(Instrument, Instrument.id == LiveSetupMarket.instrument_id)
        .outerjoin(Broker, Broker.id == Instrument.broker_id)
        .where(LiveSetup.active, LiveSetupMarket.active)
        .order_by(LiveSetup.created_at, LiveSetupMarket.created_at)
    )
    return [
        FollowedPair(
            setup_id=setup.id,
            strategy_id=setup.strategy_id,
            instrument_id=instrument_id,
            timeframe=setup.timeframe,
            cost_model=dict(setup.cost_model),
            no_target_r=setup.no_target_r,
            broker=slug,
        )
        for setup, instrument_id, slug in session.execute(statement)
    ]


# --------------------------------------------------------------------------- signals


def _uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def _decimal(raw: str | None) -> Decimal | None:
    return Decimal(raw) if raw else None


def record_event(session: Session, fields: Mapping[str, str]) -> SignalRecord:
    """Fold one `signals.events` entry into its signal's row, made at the first event seen.

    Idempotent: an entry read twice writes the same values again."""
    number = int(fields["number"])
    kind = fields["kind"]
    at = dt.datetime.fromisoformat(fields["time"]) if fields.get("time") else None
    row = session.scalars(select(SignalRecord).where(SignalRecord.number == number)).one_or_none()
    if row is None:
        row = SignalRecord(
            number=number,
            setup_id=_uuid(fields.get("setup_id")),
            strategy_id=_uuid(fields.get("strategy_id")),
            instrument_id=_uuid(fields.get("instrument_id")),
            symbol=fields.get("symbol", "?"),
            timeframe=fields.get("timeframe", ""),
            side=fields.get("side", "long"),
            status=kind,
            session_id=_uuid(fields.get("session_id")),
        )
        session.add(row)
    if fields.get("order_type"):
        row.order_type = fields["order_type"]
    for name in ("entry", "stop", "target"):
        value = _decimal(fields.get(name))
        if value is not None:
            setattr(row, name, value)
    if kind == "armed":
        row.armed_at = row.armed_at or at
    elif kind == "triggered":
        row.status = "triggered"
        row.triggered_at = at
    elif kind == "cancelled":
        row.status = "cancelled"
        row.ended_at = at
        row.reason = fields.get("reason") or None
    elif kind == "closed":
        row.status = "closed"
        row.ended_at = at
        row.exit_price = _decimal(fields.get("exit_price"))
        row.result_r = _decimal(fields.get("result_r"))
        row.reason = fields.get("reason") or None
    session.flush()
    return row


@dataclass(frozen=True, slots=True)
class OpenSignal:
    """A signal still armed or in a trade, with everything its next message needs."""

    number: int
    status: str
    symbol: str
    timeframe: str
    side: str
    entry: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    armed_at: dt.datetime | None
    triggered_at: dt.datetime | None
    session_id: uuid.UUID | None
    setup_id: uuid.UUID | None
    strategy_id: uuid.UUID | None
    instrument_id: uuid.UUID | None
    strategy: str
    broker: str | None
    no_target_r: Decimal | None


_OPEN = ("armed", "triggered")


# The outer joins make each of the three nullable; the column types say otherwise.
_OpenRow = tuple[SignalRecord, str, str, Decimal]


def _open_signals() -> Select[_OpenRow]:
    return (
        select(SignalRecord, Strategy.name, Broker.slug, LiveSetup.no_target_r)
        .outerjoin(Strategy, Strategy.id == SignalRecord.strategy_id)
        .outerjoin(Instrument, Instrument.id == SignalRecord.instrument_id)
        .outerjoin(Broker, Broker.id == Instrument.broker_id)
        .outerjoin(LiveSetup, LiveSetup.id == SignalRecord.setup_id)
        .where(SignalRecord.status.in_(_OPEN))
        .order_by(SignalRecord.number)
    )


def _as_open(
    row: SignalRecord, strategy: str | None, broker: str | None, no_target_r: Decimal | None
) -> OpenSignal:
    return OpenSignal(
        number=row.number,
        status=row.status,
        symbol=row.symbol,
        timeframe=row.timeframe,
        side=row.side,
        entry=row.entry,
        stop=row.stop,
        target=row.target,
        armed_at=row.armed_at,
        triggered_at=row.triggered_at,
        session_id=row.session_id,
        setup_id=row.setup_id,
        strategy_id=row.strategy_id,
        instrument_id=row.instrument_id,
        strategy=strategy or "",
        broker=broker,
        no_target_r=no_target_r,
    )


def orphan_signals(session: Session, alive: Collection[uuid.UUID]) -> list[OpenSignal]:
    """The signals still armed or in a trade whose session is not one of `alive` (09/10).

    Nobody is watching them any more — a restart, the machine switched off, a setup turned off —
    and without somebody to, a signal stays open on the channel and on the screen for ever."""
    statement = _open_signals()
    if alive:
        statement = statement.where(
            (SignalRecord.session_id.is_(None)) | (SignalRecord.session_id.not_in(list(alive)))
        )
    return [_as_open(*row) for row in session.execute(statement)]


def open_signal(session: Session, number: int) -> OpenSignal | None:
    """Signal `number` if it is still armed or in a trade."""
    found = session.execute(_open_signals().where(SignalRecord.number == number)).first()
    return None if found is None else _as_open(*found)


def signals_of(
    session: Session, setup_id: uuid.UUID, *, limit: int = 100, offset: int = 0
) -> list[SignalRecord]:
    """A setup's signals, newest first."""
    return list(
        session.scalars(
            select(SignalRecord)
            .where(SignalRecord.setup_id == setup_id)
            .order_by(SignalRecord.number.desc())
            .limit(limit)
            .offset(offset)
        )
    )


@dataclass(frozen=True, slots=True)
class SetupMetrics:
    """What a setup's closed signals add up to, in R."""

    signals: int
    closed: int
    open: int
    cancelled: int
    wins: int
    win_rate: Decimal | None
    net_r: Decimal
    average_r: Decimal | None
    profit_factor: Decimal | None
    max_drawdown_r: Decimal
    r_by_month: dict[str, Decimal] = field(default_factory=dict)


def setup_metrics(session: Session, setup_id: uuid.UUID) -> SetupMetrics:
    """The setup's history summed up, in R, from its signals in the order they closed."""
    rows = session.execute(
        select(SignalRecord.status, SignalRecord.result_r, SignalRecord.ended_at)
        .where(SignalRecord.setup_id == setup_id)
        .order_by(SignalRecord.ended_at.nulls_last(), SignalRecord.number)
    ).all()
    results = [r for status, r, _ in rows if status == "closed" and r is not None]
    gains = sum((r for r in results if r > 0), Decimal(0))
    losses = -sum((r for r in results if r < 0), Decimal(0))
    curve, peak, drawdown = Decimal(0), Decimal(0), Decimal(0)
    by_month: dict[str, Decimal] = {}
    for status, r, ended in rows:
        if status != "closed" or r is None:
            continue
        curve += r
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)
        if ended is not None:
            month = ended.strftime("%Y-%m")
            by_month[month] = by_month.get(month, Decimal(0)) + r
    wins = sum(1 for r in results if r > 0)
    return SetupMetrics(
        signals=len(rows),
        closed=len(results),
        open=sum(1 for status, _, _ in rows if status in {"armed", "triggered"}),
        cancelled=sum(1 for status, _, _ in rows if status == "cancelled"),
        wins=wins,
        win_rate=Decimal(wins) / len(results) if results else None,
        net_r=sum(results, Decimal(0)),
        average_r=sum(results, Decimal(0)) / len(results) if results else None,
        profit_factor=gains / losses if losses > 0 else None,
        max_drawdown_r=drawdown,
        r_by_month=by_month,
    )
