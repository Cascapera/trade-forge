"""Announcing a closed candle on a Redis stream.

**A stream, not the pub/sub channel this project already uses for job progress.** Pub/sub
delivers to whoever happens to be listening and forgets the rest, which is right for a
percentage nobody needs twice and wrong for a candle: a paper session that was restarting when
a bar closed would simply never see that bar, and its strategy would be a bar short with
nothing to show for it. A stream keeps what it was given, and a consumer group remembers where
each reader got to.

⚠️ **Idempotency is Redis's job here, not this module's.** The entry id is the candle's own
opening instant in milliseconds, so announcing the same bar twice is an id that is not greater
than the last one — which `XADD` refuses. That makes a duplicate impossible rather than
unlikely, and it survives the loop being killed and restarted, which an in-process set of
"already published" does not.
"""

import datetime as dt
from decimal import Decimal

from redis import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT

from tradeforge_collector.live import Subscription, stream_name
from tradeforge_collector.source import Candle

__all__ = ["RedisCandlePublisher", "candle_fields", "entry_id"]

# Redis trims from the oldest end, approximately, to somewhere near this many entries. A day of
# M1 bars is 1440, so this keeps roughly a week of the fastest timeframe — long enough for a
# session to be down over a weekend and still catch up, short enough that an unread stream
# cannot grow without bound.
DEFAULT_MAXLEN = 10_000


def entry_id(candle: Candle) -> str:
    """The stream id for a candle: its opening instant in milliseconds, sequence zero.

    ⚠️ The **opening** instant, matching `Candle.time`. Using the close would make the id
    disagree with the field beside it, and every consumer would have to know which of the two
    it was holding.
    """
    return f"{int(candle.time.timestamp() * 1000)}-0"


def candle_fields(subscription: Subscription, candle: Candle) -> dict[FieldT, EncodableT]:
    """The candle as a flat map of strings, which is the only shape a stream entry has.

    Typed in the client's own vocabulary rather than as `dict[str, str]`, because `dict` is
    invariant and this function exists to produce exactly what `xadd` takes — it is the encoding
    step of the transport, living in the transport's module. Spelling the union out by hand
    instead of importing it would be a copy of somebody else's type.

    Prices go out as their decimal text rather than as floats. The engine prices in `Decimal`
    precisely so that a tick is a tick, and a float round-trip at the edge would give some of
    that back for nothing — the consumer parses `Decimal(field)` and is exactly where it
    started.
    """
    return {
        "symbol": subscription.symbol,
        "timeframe": subscription.timeframe,
        "time": candle.time.isoformat(),
        "open": str(candle.open),
        "high": str(candle.high),
        "low": str(candle.low),
        "close": str(candle.close),
        "tick_volume": str(candle.tick_volume),
        "spread": str(candle.spread),
        "real_volume": str(candle.real_volume),
    }


def candle_from_fields(fields: dict[str, str]) -> Candle:
    """The inverse, for a consumer — and for the test that proves the pair is lossless."""
    return Candle(
        time=dt.datetime.fromisoformat(fields["time"]),
        open=Decimal(fields["open"]),
        high=Decimal(fields["high"]),
        low=Decimal(fields["low"]),
        close=Decimal(fields["close"]),
        tick_volume=int(fields["tick_volume"]),
        spread=int(fields["spread"]),
        real_volume=int(fields["real_volume"]),
    )


class RedisCandlePublisher:
    """Publishes closed candles to `candles.{symbol}.{timeframe}`."""

    def __init__(self, client: Redis, *, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._client = client
        self._maxlen = maxlen

    def publish(self, subscription: Subscription, candle: Candle) -> bool:
        """`True` if this bar was new, `False` if the stream already had it.

        The refusal is read from the error rather than pre-empted with a read: asking "is it
        there?" and then writing is two round trips with a race between them, and the race is
        real — two collectors on the same symbol is a configuration mistake, not an
        impossibility, and it should cost a duplicate that Redis rejects rather than one it
        accepts.
        """
        try:
            self._client.xadd(
                name=stream_name(subscription),
                fields=candle_fields(subscription, candle),
                id=entry_id(candle),
                maxlen=self._maxlen,
                approximate=True,
            )
        except ResponseError as error:
            # ⚠️ Matched on the message because the client raises one type for every server
            # error. Anything else is re-raised: a stream that has been turned into another
            # type, or a server that is out of memory, must not read as "already published".
            if "equal or smaller" in str(error).lower():
                return False
            raise
        return True
