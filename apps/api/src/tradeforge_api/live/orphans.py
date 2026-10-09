"""Signals whose session is gone, seen to their end by price (09/10).

His ask, after the first day: signal #1 (Usa500) and #2 (USDJPY) stayed open on the channel and
on the screen for good — the sessions that posted them had been stopped, one by a setup turned
off, one by a restart — while #2 had hit its stop an hour before. A signal on the channel is a
position somebody may hold; nobody watching it is not an acceptable end.

So the supervisor, every round, looks for **orphans**: a signal still armed or in a trade whose
session is not one alive now. Each launch of a session has its own id (`wanted_children`), so a
session restarted after a crash orphans what it posted before, exactly like a machine restarted.

* **In a trade** — followed bar by bar on its market's live stream, from the bar it filled on:
  its stop (the one a breakeven moved it to, when there was one), its target, or `no_target_r`
  for a setup with none. Closed with its result in R and a note saying it was followed by price.
  The setup's own exit cannot be replayed without its session; that is what the note is for.
* **Armed** — cancelled: the order it announced is no longer watched by anybody.

⚠️ **The stop is asked before the target** when one bar reached both: a bar's range says
nothing about which came first, and the cautious answer is the loss.

`annul` is the other way a signal ends without its session: by hand, for one that should never
have been posted (signal #1, armed across a week missing from the history).
"""

import datetime as dt
import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import Decimal
from typing import cast

from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.signals import SIGNALS_STREAM
from tradeforge_collector.demand import want
from tradeforge_collector.live import Subscription, stream_name
from tradeforge_db.live_setups import OpenSignal, open_signal, orphan_signals
from tradeforge_db.session import session_scope

__all__ = ["ORPHAN_NOTE", "OrphanWatch", "annul", "orphan_brokers", "resolve_orphans"]

logger = logging.getLogger(__name__)

ORPHAN_NOTE = "Acompanhado pelo preço: a sessão que anunciou este sinal foi reiniciada."

_LEASE = 90.0
"""Seconds a market is asked of its live loop for an orphan in a trade — renewed every round."""


@dataclass(slots=True)
class OrphanWatch:
    """What the supervisor remembers between rounds, so a round reads only what is new."""

    checked: dict[int, dt.datetime] = field(default_factory=dict)
    """The last bar already looked at, per signal."""
    moved: dict[int, Decimal | None] = field(default_factory=dict)
    """Where a breakeven put each signal's stop — `None` when it never moved."""
    ended: set[int] = field(default_factory=set)
    """Ended this process: not posted twice while the history catches up."""


def _ms(moment: dt.datetime) -> str:
    return str(int(moment.timestamp() * 1000))


def _context(one: OpenSignal) -> dict[str, str]:
    """The fields every entry of a signal carries, as `RedisSignalSink` writes them."""
    return {
        "session_id": "" if one.session_id is None else str(one.session_id),
        "setup_id": "" if one.setup_id is None else str(one.setup_id),
        "strategy_id": "" if one.strategy_id is None else str(one.strategy_id),
        "instrument_id": "" if one.instrument_id is None else str(one.instrument_id),
        "strategy": one.strategy,
        "timeframe": one.timeframe,
        "broker": one.broker or "",
        "no_target_r": "" if one.no_target_r is None else str(one.no_target_r),
        "number": str(one.number),
        "symbol": one.symbol,
        "side": one.side,
        "entry": "" if one.entry is None else str(one.entry),
        "stop": "" if one.stop is None else str(one.stop),
        "target": "" if one.target is None else str(one.target),
    }


def _post(redis: Redis, fields: dict[str, str]) -> None:
    redis.xadd(
        SIGNALS_STREAM,
        {key: value for key, value in fields.items() if value != ""},
        maxlen=100_000,
    )


def _moved_stop(redis: Redis, one: OpenSignal) -> Decimal | None:
    """Where this signal's breakeven put its stop, read back from the stream; `None` if never."""
    since = one.triggered_at or one.armed_at
    entries = cast(
        "list[tuple[str, dict[str, str]]]",
        redis.xrange(SIGNALS_STREAM, min=_ms(since) if since is not None else "-"),
    )
    for _entry_id, fields in entries:
        if fields.get("kind") == "breakeven" and fields.get("number") == str(one.number):
            return Decimal(fields["moved_stop"]) if fields.get("moved_stop") else None
    return None


def _exit(
    one: OpenSignal, *, entry: Decimal, first_stop: Decimal, stop: Decimal, bar: dict[str, str]
) -> tuple[Decimal, Decimal] | None:
    """`(exit price, result in R)` if this bar ended the trade, else `None`. Stop first.

    R is measured against `first_stop` whatever `stop` is now — a breakeven makes it 0."""
    long = one.side == "long"
    risk = abs(entry - first_stop)
    high, low = Decimal(bar["high"]), Decimal(bar["low"])
    target = one.target
    if target is None and one.no_target_r is not None and risk > 0:
        reach = one.no_target_r * risk
        target = entry + reach if long else entry - reach
    if (low <= stop) if long else (high >= stop):
        price = stop
    elif target is not None and ((high >= target) if long else (low <= target)):
        price = target
    else:
        return None
    moved = price - entry if long else entry - price
    return price, (moved / risk if risk > 0 else Decimal(0))


def _follow(redis: Redis, one: OpenSignal, watch: OrphanWatch) -> dict[str, str] | None:
    """The closing entry for an orphan in a trade, once a bar ends it; `None` until then."""
    if one.entry is None or one.stop is None or one.triggered_at is None:
        return {
            **_context(one),
            "kind": "cancelled",
            "time": dt.datetime.now(dt.UTC).isoformat(),
            "clock": "wall",
            "reason": "sessão reiniciada sem os níveis do trade para acompanhar",
        }
    if one.number not in watch.moved:
        watch.moved[one.number] = _moved_stop(redis, one)
    stop = watch.moved[one.number] or one.stop
    since = watch.checked.get(one.number, one.triggered_at)
    bars = cast(
        "list[tuple[str, dict[str, str]]]",
        redis.xrange(stream_name(Subscription(one.symbol, one.timeframe)), min=_ms(since)),
    )
    for _entry_id, bar in bars:
        at = dt.datetime.fromisoformat(bar["time"])
        if at <= since:
            continue  # the fill bar's own range is not known to come after the fill
        watch.checked[one.number] = at
        ended = _exit(one, entry=one.entry, first_stop=one.stop, stop=stop, bar=bar)
        if ended is not None:
            price, result = ended
            return {
                **_context(one),
                "kind": "closed",
                "time": at.isoformat(),
                "exit_price": str(price),
                "result_r": str(result.quantize(Decimal("0.01"))),
                "reason": "acompanhado pelo preço após a sessão parar",
                "note": ORPHAN_NOTE,
            }
    return None


def resolve_orphans(
    redis: Redis,
    factory: sessionmaker[Session],
    alive: Collection[uuid.UUID],
    watch: OrphanWatch,
) -> int:
    """Post the end of every orphan that has one; how many it posted.

    The market of an orphan in a trade is asked of its broker's live loop on every round, so its
    bars keep coming after the setup that wanted them was turned off (`orphan_brokers` keeps the
    loop itself running)."""
    with session_scope(factory) as session:
        orphans = orphan_signals(session, alive)
    posted = 0
    for one in orphans:
        if one.number in watch.ended:
            continue
        if one.status == "armed":
            fields: dict[str, str] | None = {
                **_context(one),
                "kind": "cancelled",
                "time": dt.datetime.now(dt.UTC).isoformat(),
                "clock": "wall",
                "reason": "sessão reiniciada: a ordem deixou de ser acompanhada",
            }
        else:
            if one.broker is not None:
                want(redis, one.broker, Subscription(one.symbol, one.timeframe), lease=_LEASE)
            fields = _follow(redis, one, watch)
        if fields is None:
            continue
        _post(redis, fields)
        watch.ended.add(one.number)
        posted += 1
        logger.info("signal #%d (%s): %s, its session gone", one.number, one.symbol, fields["kind"])
    return posted


def orphan_brokers(factory: sessionmaker[Session], alive: Collection[uuid.UUID]) -> set[str]:
    """The brokers whose live loop an orphan in a trade still needs."""
    with session_scope(factory) as session:
        return {
            one.broker
            for one in orphan_signals(session, alive)
            if one.status == "triggered" and one.broker is not None
        }


def annul(redis: Redis, factory: sessionmaker[Session], number: int, reason: str) -> bool:
    """Withdraw signal `number` from the channel and from the metrics; `False` if it is not open.

    For a signal that should never have been posted — not a loss, which stays a loss."""
    with session_scope(factory) as session:
        one = open_signal(session, number)
    if one is None:
        return False
    _post(
        redis,
        {
            **_context(one),
            "kind": "cancelled",
            "annulled": "1",
            "time": dt.datetime.now(dt.UTC).isoformat(),
            "clock": "wall",
            "reason": reason,
        },
    )
    return True
