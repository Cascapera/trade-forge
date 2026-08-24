"""The live loop: notice that a candle closed, say so once, and survive the terminal going away.

Backfill answers "what happened between these two instants". This answers a different
question — "has the bar I am watching finished?" — and the difference is not the data, it is
what decides. A shut market, a stale feed and a broker clock that is not a clock all look the
same from the outside, so the rule here is that **the loop never asks a clock whether a bar
closed**.

MetaTrader has no push API. `copy_rates_from_pos` is a poll, and the position is the answer:
index 0 is the bar still forming, index 1 is the last one that closed. The source is asked by
position and nothing else, so "closed" is a fact the terminal states rather than an inference
this module draws.

⚠️ **The alternative is what makes this worth writing down.** The obvious loop compares
`bar.time + step <= now`, which needs the broker's offset from UTC — and that offset is
measured from the newest tick, so a market that stopped ticking makes it wrong by exactly the
length of the stop. Measured on this project's own broker on 18/08/2026: five symbols all
stopped at 22:59:59 server time, and 46 minutes later the measured offset read +134 min
instead of the real +180. A loop deciding "closed?" with that number decides wrong for as long
as the market is shut, and nothing raises. The offset is still needed to *label* a bar in UTC
— but labelling is a translation, and deciding is not.

## Losing the terminal, and the hole that leaves

A loop that only ever asks for the **last** closed bar publishes a stream with holes in it,
because the world does not stop while the collector is down. Thirty minutes off the air on M1
is thirty bars that closed and one that gets announced — and the twenty-nine in between are
not lost loudly, they are simply absent. A paper session reading that stream sees a market
that skipped, with nothing saying so.

So the loop remembers where it got to and, when the bar it is handed is not the successor of
that one, asks for the run of bars in between. Three things about how:

* **By position again, never by range.** `candles(start, end)` exists and would have been the
  short way to write this, and it is the same trap as above: translating those bounds into the
  server's clock makes the contaminated offset *decide which window to ask for*, so a shut
  market fetches the wrong bars and the hole survives the repair.
* **How many is derived from two bar timestamps**, both stated by the terminal — no clock is
  consulted. Over a market closure that count overshoots, because wall-clock time passed
  without bars being made; overshooting is free (see the next point) and undershooting would
  leave exactly the hole this is here to close.
* **Idempotency is still Redis's job.** Everything fetched is published, oldest first, and the
  stream refuses the ids it already has. There is no second opinion about what is new, and the
  gap fill needs no bookkeeping of its own.

The consequence worth naming: **filling a gap is not a special path for reconnection.** A
reconnection is one cause of a hole; the process being restarted is another, and a symbol that
went quiet over a holiday is a third. They all arrive at the same comparison, so they are all
repaired by the same few lines.
"""

import datetime as dt
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tradeforge_collector.source import Candle
from tradeforge_collector.timeframes import step

__all__ = [
    "DEFAULT_MAX_BACKFILL",
    "Backoff",
    "CandlePublisher",
    "LiveSource",
    "Subscription",
    "poll_once",
    "run",
    "stream_name",
]

logger = logging.getLogger(__name__)

# How many bars one gap fill will ask for. A ceiling exists because the count is derived from
# elapsed time, and elapsed time over a weekend is thousands of bars of which almost none are
# real. 500 covers a full trading day of M1 and any plausible outage of a slower timeframe;
# past that the honest repair is a backfill, and the loop says so instead of pretending.
DEFAULT_MAX_BACKFILL = 500


@dataclass(frozen=True, slots=True)
class Subscription:
    """One symbol on one timeframe — the unit the loop watches and publishes."""

    symbol: str
    timeframe: str


@dataclass(frozen=True, slots=True)
class Backoff:
    """How long to wait before the n-th consecutive attempt to get the terminal back.

    A pure function of the attempt number, and that is the whole reason it is a type rather
    than four lines inside the loop: retry policy that is only reachable by actually failing,
    actually sleeping and actually reconnecting is policy nobody tests. Here the schedule can
    be asserted in a millisecond, and the loop's job shrinks to counting.

    Doubling with a ceiling, rather than a fixed delay: a terminal restarted by hand is back in
    seconds and should be picked up in seconds, while a machine that will be down until morning
    must not spend the night reconnecting once a second.
    """

    first: float = 1.0
    cap: float = 60.0

    def delay(self, attempt: int) -> float:
        """Seconds to wait before `attempt`, counting from 1."""
        if attempt < 1:
            raise ValueError(f"attempts are counted from 1, got {attempt}")
        return min(self.first * 2.0 ** (attempt - 1), self.cap)


# The default retry schedule, as one shared value rather than a call in `run`'s signature.
# `Backoff` is frozen, so sharing it cannot leak state between two loops in one process.
DEFAULT_BACKOFF = Backoff()


@runtime_checkable
class LiveSource(Protocol):
    """A source that can be watched: subscribed to, asked for closed bars, and reattached.

    ⚠️ `runtime_checkable` so a caller holding a plain `MarketDataSource` can *ask* — an
    `isinstance` here checks that the methods exist, which is exactly what conforming
    structurally means. It is what lets the CLI refuse a source that cannot watch a market with
    a sentence, instead of reaching the first poll and failing on a missing attribute.

    Separate from `MarketDataSource` rather than bolted onto it, because they are answers to
    different questions and only one of them is available on a Linux box with no terminal.
    Structural, like its neighbour: `MT5Source` satisfies both by having both sets of methods,
    and no class in this package inherits from either.
    """

    def subscribe(self, symbol: str) -> None:
        """Make sure this symbol will actually answer, or raise saying why it will not.

        ⚠️ **This exists because of a measurement.** On this project's broker only 5 of 9550
        symbols were in Market Watch, and a symbol that is not there answers "no bars" for
        ever — which is a legitimate answer for a symbol that has not closed one yet, and so
        is indistinguishable from it. The loop would poll a dead subscription until somebody
        noticed by hand. Selecting the symbol up front turns that silence into a refusal at
        start-up.
        """
        ...

    def recent_closed(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """The last `count` **closed** bars, oldest first. Fewer, or none, is a real answer.

        A symbol the broker has never quoted, and a session that has not produced its first
        bar, both legitimately have no closed bar yet — so an empty list is an answer and not
        a failure. A list rather than an optional single bar, because "the last one" and "the
        last few" are the same question asked by position, and two methods for one question is
        one of them going stale.

        ⚠️ Raises `ConnectionError`, and only that, when the source itself has gone away. That
        distinction is the whole reconnection design: any other failure is about one symbol,
        this one is about the feed.
        """
        ...

    def reconnect(self) -> None:
        """Attach again after the connection was lost. May raise; the caller retries.

        The *mechanism* lives here and the *policy* — how often, how long, how many times —
        lives in `run`, on purpose. Policy inside the MetaTrader adapter is policy that only
        runs on a Windows box with a terminal, which means it is observed once, by hand, and
        never again.
        """
        ...


class CandlePublisher(Protocol):
    """Somewhere a closed candle can be announced, and which remembers what it announced."""

    def publish(self, subscription: Subscription, candle: Candle) -> bool:
        """Announce it. `True` if this was new, `False` if it had already been announced.

        The distinction is the publisher's to make and not the loop's, because the only
        durable answer lives wherever the candles are being written — a loop that restarted
        remembers nothing, and one that kept its own set would announce every bar again.
        """
        ...

    def last_published(self, subscription: Subscription) -> dt.datetime | None:
        """The opening instant of the newest bar already announced, or `None` for a new stream.

        This is what a restarted loop resumes from, and it deliberately comes from the same
        place the candles do. A file, or a row in some other table, would be a second record of
        the same fact — and two records of one fact disagree the first time a process is killed
        between writing them.
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
    max_backfill: int = DEFAULT_MAX_BACKFILL,
) -> Mapping[Subscription, list[Candle]]:
    """Ask every subscription what has closed since last time and publish what is new.

    Returns what was published, keyed by subscription — the empty mapping being the normal
    answer most of the time, because a poll is far more frequent than a bar. A list per
    subscription rather than a single candle, because a poll that follows an outage announces
    the whole run at once.

    `seen` is an optional in-process memory of the last bar published per subscription. It is
    an optimisation and never the guarantee: the publisher is what decides newness, so a loop
    that restarted with an empty `seen` republishes at most one bar per subscription and the
    publisher refuses it. Making this the guarantee instead would put the only record of what
    was announced in a process that is expected to be killed and restarted.

    ⚠️ Without `seen` there is nothing to measure a gap against, so gap filling is off — which
    is honest rather than limited, because "the last bar" is all a caller who kept no position
    can ask for. `run` seeds it from the stream for exactly this reason.

    One symbol failing does not stop the others. A live loop that stopped collecting EURUSD
    because a stock's session ended would be a single symbol taking the whole feed down. A
    `ConnectionError` is the exception to that and propagates: it is not a statement about one
    symbol, it is the feed, and retrying the remaining subscriptions against a terminal that
    has gone away only delays the reconnection.
    """
    published: dict[Subscription, list[Candle]] = {}

    for subscription in subscriptions:
        try:
            bars = _closed_since(source, subscription, seen=seen, max_backfill=max_backfill)
        except ConnectionError:
            raise
        except Exception:
            logger.exception(
                "could not read the last closed bar for %s %s",
                subscription.symbol,
                subscription.timeframe,
            )
            continue

        if not bars:
            continue

        fresh = [candle for candle in bars if publisher.publish(subscription, candle)]
        if fresh:
            published[subscription] = fresh
            logger.info(
                "%s %s: published %d candle(s), up to %s",
                subscription.symbol,
                subscription.timeframe,
                len(fresh),
                fresh[-1].time,
            )
        if seen is not None:
            seen[subscription] = bars[-1].time

    return published


def _closed_since(
    source: LiveSource,
    subscription: Subscription,
    *,
    seen: Mapping[Subscription, dt.datetime] | None,
    max_backfill: int,
) -> list[Candle]:
    """Every bar this subscription closed that has not been announced yet, oldest first.

    Two calls in the gap case and one otherwise, which is the point of doing it this way: the
    size of a hole cannot be known before the newest bar is in hand, and the alternative —
    guessing the count from the wall clock on every poll — would ask for thousands of bars
    every Monday morning to discover that nothing had been missed.
    """
    latest = source.recent_closed(subscription.symbol, subscription.timeframe, 1)
    if not latest:
        return []

    newest = latest[-1]
    last = None if seen is None else seen.get(subscription)
    if last is None:
        return latest
    if newest.time <= last:
        return []

    bar = step(subscription.timeframe)
    # Integer division of two timedeltas: 1 when this bar is the successor of the last one, and
    # exactly the number of bars owed when it is not. Both operands are instants the terminal
    # stated, so no clock — and no offset — takes part in the decision.
    owed = (newest.time - last) // bar
    if owed <= 1:
        return latest

    wanted = min(owed, max_backfill)
    logger.warning(
        "%s %s: %d bar(s) owed since %s; asking the terminal for the last %d by position",
        subscription.symbol,
        subscription.timeframe,
        owed,
        last,
        wanted,
    )
    filled = source.recent_closed(subscription.symbol, subscription.timeframe, wanted)
    if not filled:
        return latest

    _warn_if_short(subscription, filled=filled, last=last, bar=bar)
    return filled


def _warn_if_short(
    subscription: Subscription,
    *,
    filled: Sequence[Candle],
    last: dt.datetime,
    bar: dt.timedelta,
) -> None:
    """Say so when the fetch did not reach back far enough to close the hole.

    ⚠️ A market closure does not trip this, and the reason is worth knowing: positions are
    contiguous over the bars that **exist**, so asking for the last 500 closed bars on a Sunday
    evening reaches back through Friday's session rather than into an empty weekend. The count
    overshoots in time and the fetch lands early, which is precisely the outcome that makes
    overshooting safe. What is left is the genuine case — an outage longer than the ceiling —
    and it is announced with the interval that is missing rather than quietly truncated, since
    a stream that skips is a strategy that never saw those bars.
    """
    oldest = filled[0].time
    if oldest - last <= bar:
        return
    logger.warning(
        "%s %s: the stream is missing %s to %s - the gap is longer than one fill can reach. "
        "Re-run a backfill over that interval if those bars matter.",
        subscription.symbol,
        subscription.timeframe,
        last + bar,
        oldest - bar,
    )


def run(  # noqa: PLR0913 — six of the nine are seams a test injects, not knobs an operator turns
    source: LiveSource,
    publisher: CandlePublisher,
    subscriptions: Sequence[Subscription],
    *,
    every: float,
    polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Backoff = DEFAULT_BACKOFF,
    max_backfill: int = DEFAULT_MAX_BACKFILL,
) -> int:
    """Watch these subscriptions until the poll budget runs out, or for ever.

    Returns how many candles were published.

    `polls=None` is the real thing: a loop with no natural end, meant to be killed. `polls=1`
    is the smoke test, and any other number is a test that wants to watch the loop lose the
    terminal and get it back — which is what `sleep` being injectable is for. A reconnection
    test that actually slept would be a test nobody runs.

    ⚠️ **A `ConnectionError` on the last permitted poll is re-raised rather than retried.** A
    single-shot run against a dead terminal must exit as a failure; swallowing it would make
    `--once` report success for a collector that collected nothing.
    """
    seen: dict[Subscription, dt.datetime] = {}
    _resume(publisher, subscriptions, seen)
    try:
        _subscribe(source, subscriptions)
    except ConnectionError as error:
        # ⚠️ Starting before the terminal is up is boot order, not a mistake, and it is the
        # same condition the loop below already knows how to wait out. Dying here would mean a
        # collector that needs a human whenever it happens to win the race against MetaTrader.
        #
        # Only `ConnectionError`. A symbol the terminal refuses is configuration — a typo, or a
        # instrument this account cannot see — and retrying that for ever would hide it behind
        # a log line nobody reads.
        logger.warning("could not select the symbols yet (%s); the loop will retry", error)

    total = 0
    failures = 0
    polls_left = polls

    while True:
        # Worked out before the poll, because it decides what a failure *means*: on any other
        # round a dead terminal costs a retry, and on this one there is no round left to
        # recover in, so the caller has to be told.
        last_round = polls_left is not None and polls_left <= 1

        try:
            published = poll_once(
                source, publisher, subscriptions, seen=seen, max_backfill=max_backfill
            )
        except ConnectionError as error:
            if last_round:
                raise
            failures += 1
            delay = backoff.delay(failures)
            logger.warning(
                "the terminal is not answering (%s); reconnecting in %.0fs (attempt %d)",
                error,
                delay,
                failures,
            )
            sleep(delay)
            _reconnect(source, subscriptions)
        else:
            failures = 0
            total += sum(len(bars) for bars in published.values())
            if last_round:
                break
            sleep(every)

        if polls_left is not None:
            polls_left -= 1

    return total


def _resume(
    publisher: CandlePublisher,
    subscriptions: Iterable[Subscription],
    seen: dict[Subscription, dt.datetime],
) -> None:
    """Seed `seen` from the streams, so a restarted process knows what it owes.

    Without this a restart looks exactly like a first run: nothing to compare against, so no
    gap is detectable and the bars that closed while the process was down are never asked for.
    The record is read back from the stream because that is where it is durable — a loop's own
    memory of what it announced dies with the loop, which is the failure this repairs.
    """
    for subscription in subscriptions:
        last = publisher.last_published(subscription)
        if last is None:
            continue
        seen[subscription] = last
        logger.info("%s %s: resuming after %s", subscription.symbol, subscription.timeframe, last)


def _subscribe(source: LiveSource, subscriptions: Iterable[Subscription]) -> None:
    """Select every distinct symbol once, whatever the timeframes ask for."""
    for symbol in dict.fromkeys(subscription.symbol for subscription in subscriptions):
        source.subscribe(symbol)


def _reconnect(source: LiveSource, subscriptions: Iterable[Subscription]) -> bool:
    """Try to get the feed back, and re-select the symbols if it comes back.

    ⚠️ Failing is expected and must not end the loop — a terminal that is being restarted is
    unreachable for as long as it takes, and a collector that gave up on the first refusal
    would need a human for something that fixes itself. Re-selecting after a reconnection
    rather than trusting the terminal to have remembered: Market Watch is terminal state, a
    reattached session is a fresh conversation, and re-selecting a symbol that is already there
    costs nothing.
    """
    try:
        source.reconnect()
        _subscribe(source, subscriptions)
    except Exception:  # anything at all here must cost a retry, never the loop
        logger.warning("reconnecting failed; will try again", exc_info=True)
        return False
    logger.info("reconnected")
    return True
