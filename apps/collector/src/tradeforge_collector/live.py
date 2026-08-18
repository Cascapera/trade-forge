"""The live loop: notice that a candle closed, and say so once.

Backfill answers "what happened between these two instants". This answers a different
question — "has the bar I am watching finished?" — and the difference is not the data, it is
what decides. A shut market, a stale feed and a broker clock that is not a clock all look the
same from the outside, so the rule here is that **the loop never asks a clock whether a bar
closed**.

MetaTrader has no push API. `copy_rates_from_pos` is a poll, and the position is the answer:
index 0 is the bar still forming, index 1 is the last one that closed. The source is asked for
index 1 and nothing else, so "closed" is a fact the terminal states rather than an inference
this module draws.

⚠️ **The alternative is what makes this worth writing down.** The obvious loop compares
`bar.time + step <= now`, which needs the broker's offset from UTC — and that offset is
measured from the newest tick, so a market that stopped ticking makes it wrong by exactly the
length of the stop. Measured on this project's own broker on 18/08/2026: five symbols all
stopped at 22:59:59 server time, and 46 minutes later the measured offset read +134 min
instead of the real +180. A loop deciding "closed?" with that number decides wrong for as long
as the market is shut, and nothing raises. The offset is still needed to *label* a bar in UTC
— but labelling is a translation, and deciding is not.
"""

import datetime as dt
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tradeforge_collector.source import Candle

__all__ = [
    "CandlePublisher",
    "LiveSource",
    "Subscription",
    "poll_once",
    "stream_name",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Subscription:
    """One symbol on one timeframe — the unit the loop watches and publishes."""

    symbol: str
    timeframe: str


@runtime_checkable
class LiveSource(Protocol):
    """A source that can be asked for the bar that just closed.

    ⚠️ `runtime_checkable` so a caller holding a plain `MarketDataSource` can *ask* — an
    `isinstance` here checks that the method exists, which is exactly what conforming
    structurally means. It is what lets the CLI refuse a source that cannot watch a market with
    a sentence, instead of reaching the first poll and failing on a missing attribute.

    Separate from `MarketDataSource` rather than bolted onto it, because they are answers to
    different questions and only one of them is available on a Linux box with no terminal.
    Structural, like its neighbour: `MT5Source` satisfies both by having both methods, and no
    class in this package inherits from either.
    """

    def latest_closed(self, symbol: str, timeframe: str) -> Candle | None:
        """The most recent **closed** bar, or `None` when the source has none to give.

        `None` is a real answer, not a failure: a symbol the broker has never quoted, and a
        session that has not produced its first bar, both legitimately have no closed bar yet.
        """
        ...


class CandlePublisher(Protocol):
    """Somewhere a closed candle can be announced."""

    def publish(self, subscription: Subscription, candle: Candle) -> bool:
        """Announce it. `True` if this was new, `False` if it had already been announced.

        The distinction is the publisher's to make and not the loop's, because the only
        durable answer lives wherever the candles are being written — a loop that restarted
        remembers nothing, and one that kept its own set would announce every bar again.
        """
        ...


def stream_name(subscription: Subscription) -> str:
    """`candles.{symbol}.{timeframe}` — one stream per thing a session might subscribe to.

    Per symbol *and* timeframe rather than one stream for everything: a paper session trading
    EURUSD on M5 should not have to read, and skip, every bar of every other subscription to
    find its own. The cost is more streams, which Redis does not mind.
    """
    return f"candles.{subscription.symbol}.{subscription.timeframe}"


def poll_once(
    source: LiveSource,
    publisher: CandlePublisher,
    subscriptions: Iterable[Subscription],
    *,
    seen: dict[Subscription, dt.datetime] | None = None,
) -> Mapping[Subscription, Candle]:
    """Ask every subscription for its last closed bar and publish the ones that are new.

    Returns what was published, keyed by subscription — the empty mapping being the normal
    answer most of the time, because a poll is far more frequent than a bar.

    `seen` is an optional in-process memory of the last bar published per subscription. It is
    an optimisation and never the guarantee: the publisher is what decides newness, so a loop
    that restarted with an empty `seen` republishes at most one bar per subscription and the
    publisher refuses it. Making this the guarantee instead would put the only record of what
    was announced in a process that is expected to be killed and restarted.

    One symbol failing does not stop the others. A live loop that stopped collecting EURUSD
    because a stock's session ended would be a single symbol taking the whole feed down.
    """
    published: dict[Subscription, Candle] = {}
    for subscription in subscriptions:
        try:
            candle = source.latest_closed(subscription.symbol, subscription.timeframe)
        except Exception:
            logger.exception(
                "could not read the last closed bar for %s %s",
                subscription.symbol,
                subscription.timeframe,
            )
            continue

        if candle is None:
            continue
        if seen is not None and seen.get(subscription) == candle.time:
            continue

        if publisher.publish(subscription, candle):
            published[subscription] = candle
            logger.info(
                "%s %s closed at %s", subscription.symbol, subscription.timeframe, candle.time
            )
        if seen is not None:
            seen[subscription] = candle.time

    return published
