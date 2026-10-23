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

## A broker that rewrites what was already published (14/10)

XP's `WIN$` is adjusted by a ratio at every roll: when WINV26 expired, every past bar moved up
2.1 %. The bars this loop had published, and the history the sessions warmed on, stayed in the
old scale; the next bar came in the new one, and the step read as a break of structure the market
never made. So, given an `on_rescale`, the loop remembers the last `MEMORY` bars it published (or,
for a pair it starts watching, the newest of the stream's and the disk's) and, whenever a new bar
closes, reads them again with it and compares (`rescale.compare`). Rewritten by one factor, the
new bar is **held back** and `on_rescale` is told — which collects the series again and has the
sessions restarted on it; once it has, the pair is watched again from scratch. A session never
sees a bar in the new scale over a history in the old.
"""

import datetime as dt
import logging
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tradeforge_collector.rescale import Comparison, compare
from tradeforge_collector.source import Candle
from tradeforge_collector.timeframes import step

__all__ = [
    "DEFAULT_MAX_BACKFILL",
    "DEFAULT_MAX_CATCH_UP",
    "MEMORY",
    "Backoff",
    "CandlePublisher",
    "LiveSource",
    "RescaledError",
    "Subscription",
    "TickerLiveSource",
    "poll_once",
    "run",
    "run_on_demand",
    "stream_name",
]

logger = logging.getLogger(__name__)

# How many bars one gap fill will ask for. A ceiling exists because the count is derived from
# elapsed time, and elapsed time over a weekend is thousands of bars of which almost none are
# real. 500 covers a full trading day of M1 and any plausible outage of a slower timeframe;
# past that the honest repair is a backfill, and the loop says so instead of pretending.
DEFAULT_MAX_BACKFILL = 500

# How many bars the first fill of a pair may ask for, when it starts from the last bar on disk
# rather than from the stream (09/10). The disk can be weeks behind — the collection jobs fill it,
# not this loop — and a session that warmed on it trades the hole as if it were a move: 20 000 bars
# is ten weeks of M5, read in one call.
DEFAULT_MAX_CATCH_UP = 20_000

MEMORY = 30
"""Bars of each pair the loop remembers having published and reads again at every new bar, to
find out whether the broker rewrote them (14/10): enough for `rescale.ENOUGH` to be conclusive,
few enough that a poll which found a bar asks for thirty more rows, not thousands."""

Remembered = MutableMapping["Subscription", deque[Candle]]
"""What the loop published last, per pair — what a broker that rewrote its past is caught by."""

OnRescale = Callable[["Subscription", Comparison], bool]
"""Told that the broker rewrote a pair's past; `True` once the series is collected again and its
sessions restarted, and the pair can be watched again from scratch."""


class RescaledError(Exception):
    """The broker's bars for a pair are no longer the ones this loop published: one factor away."""

    def __init__(
        self, subscription: "Subscription", comparison: Comparison, fresh: list[Candle]
    ) -> None:
        super().__init__(
            f"{subscription.symbol} {subscription.timeframe}: the broker's bars are "
            f"{comparison.ratio:.4f} times the ones published"
        )
        self.subscription = subscription
        self.comparison = comparison
        self.fresh = fresh
        """The bars just read, the new ones last — what is published if nobody can mend it."""


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


def poll_once(  # noqa: PLR0913 — keyword-only; the rescale's three are given together or not at all
    source: LiveSource,
    publisher: CandlePublisher,
    subscriptions: Iterable[Subscription],
    *,
    seen: dict[Subscription, dt.datetime] | None = None,
    max_backfill: int = DEFAULT_MAX_BACKFILL,
    memory: Remembered | None = None,
    on_rescale: OnRescale | None = None,
    rescaled: set[Subscription] | None = None,
) -> Mapping[Subscription, list[Candle]]:
    """Ask every subscription what has closed since last time and publish what is new.

    Returns what was published, keyed by subscription — the empty mapping being the normal
    answer most of the time, because a poll is far more frequent than a bar. A list per
    subscription rather than a single candle, because a poll that follows an outage announces
    the whole run at once.

    With `memory` and `on_rescale` (14/10), every new bar comes with the remembered ones read
    again; a pair whose past the broker rewrote publishes nothing this poll and joins `rescaled`
    once `on_rescale` has mended it — or, if it could not, publishes as before and is remembered
    in the new scale, so it is not caught again at every bar.

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
        if rescaled is not None and any(one.symbol == subscription.symbol for one in rescaled):
            # Mended a moment ago for another timeframe: every one of the symbol was collected
            # again, and is watched again from scratch next round.
            continue
        try:
            bars = _closed_since(
                source, subscription, seen=seen, max_backfill=max_backfill, memory=memory
            )
        except ConnectionError:
            raise
        except RescaledError as rewritten:
            logger.warning("%s; the new bar is held back", rewritten)
            if on_rescale is not None and on_rescale(subscription, rewritten.comparison):
                if rescaled is not None:
                    rescaled.add(subscription)
                continue
            logger.error(
                "%s %s: the series could not be collected again; publishing in the new scale "
                "over sessions warmed in the old one — restart them by hand",
                subscription.symbol,
                subscription.timeframe,
            )
            last = None if seen is None else seen.get(subscription)
            bars = [candle for candle in rewritten.fresh if last is None or candle.time > last]
            if memory is not None:
                memory[subscription] = deque(rewritten.fresh, maxlen=MEMORY)
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
        if memory is not None:
            memory.setdefault(subscription, deque(maxlen=MEMORY)).extend(bars)

    return published


def _closed_since(
    source: LiveSource,
    subscription: Subscription,
    *,
    seen: Mapping[Subscription, dt.datetime] | None,
    max_backfill: int,
    memory: Remembered | None = None,
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
    remembered = None if memory is None else memory.get(subscription)
    if owed <= 1 and not remembered:
        return latest

    wanted = min(owed, max_backfill)
    if owed > 1:
        logger.warning(
            "%s %s: %d bar(s) owed since %s; asking the terminal for the last %d by position",
            subscription.symbol,
            subscription.timeframe,
            owed,
            last,
            wanted,
        )
    # The remembered bars are read again with the new ones, by position like everything here:
    # MEMORY more reach back past `last` over the bars that exist.
    filled = source.recent_closed(
        subscription.symbol, subscription.timeframe, wanted + (MEMORY if remembered else 0)
    )
    if not filled:
        return latest
    if remembered:
        again = compare(list(remembered), [candle for candle in filled if candle.time <= last])
        if again.rescaled:
            raise RescaledError(subscription, again, filled)

    if owed > 1:
        _warn_if_short(subscription, filled=filled, last=last, bar=bar)
    # Asked by position, so it reaches back past `last` whenever the market was shut for part of
    # the hole: only what came after is owed.
    return [candle for candle in filled if candle.time > last]


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


def _by_name(subscription: Subscription) -> tuple[str, str]:
    return (subscription.symbol, subscription.timeframe)


def run_on_demand(  # noqa: PLR0913 — the same seams as `run`, plus where the demand comes from
    source: LiveSource,
    publisher: CandlePublisher,
    demand: Callable[[], Sequence[Subscription]],
    *,
    every: float,
    polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Backoff = DEFAULT_BACKOFF,
    max_backfill: int = DEFAULT_MAX_BACKFILL,
    on_disk: Callable[[Subscription], dt.datetime | None] | None = None,
    max_catch_up: int = DEFAULT_MAX_CATCH_UP,
    tail: Callable[[Subscription], Sequence[Candle]] | None = None,
    on_rescale: OnRescale | None = None,
) -> int:
    """`run`, watching whatever `demand` answers each round instead of a fixed list (ADR-0032).

    A pair whose stream holds nothing yet starts from `on_disk` — the last bar the collection
    stored — and is caught up from there at once, up to `max_catch_up` bars (09/10). Without it a
    session warms on the disk, receives today's bar next, and reads the hole as a move.

    One broker's live loop: a pair asked for starts being watched on the next round — resumed
    from its stream like a restart, so a pair asked for again after a pause gets its gap filled —
    and a pair no longer asked for stops. Returns how many candles were published.

    With `on_rescale` (14/10) the loop watches for a broker that rewrites a pair's past: each
    pair starts remembering `tail` (the newest of the stream's and the disk's last bars), and a
    rewrite holds its new bar back until `on_rescale` has collected the series again and had its
    sessions restarted; then every pair of that symbol is watched again from scratch.

    ⚠️ A pair the terminal refuses to select is set aside, not retried every five seconds, until
    it drops out of the demand: a refusal is configuration (a ticker the account cannot see), and
    hammering it would bury the log line that says so. A `ConnectionError` while selecting is the
    feed, not the pair, and is retried like any other.
    """
    seen: dict[Subscription, dt.datetime] = {}
    watching: set[Subscription] = set()
    refused: set[Subscription] = set()
    memory: Remembered | None = None if on_rescale is None else {}
    rescaled: set[Subscription] = set()
    total = 0
    failures = 0
    polls_left = polls

    while True:
        last_round = polls_left is not None and polls_left <= 1
        try:
            _forget_rescaled(rescaled, watching, seen, memory)
            asked = set(demand())
            for gone in (watching | refused) - asked:
                seen.pop(gone, None)
                logger.info("%s %s: no longer asked for", gone.symbol, gone.timeframe)
            watching &= asked
            refused &= asked
            for new in sorted(asked - watching - refused, key=_by_name):
                total += _start_watching(
                    new,
                    source=source,
                    publisher=publisher,
                    seen=seen,
                    watching=watching,
                    refused=refused,
                    on_disk=on_disk,
                    max_catch_up=max_catch_up,
                    rescue=_Rescue(memory, tail, on_rescale, rescaled),
                )
            published = poll_once(
                source,
                publisher,
                sorted(watching - rescaled, key=_by_name),
                seen=seen,
                max_backfill=max_backfill,
                memory=memory,
                on_rescale=on_rescale,
                rescaled=rescaled,
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
            _reconnect(source, watching)
        else:
            failures = 0
            total += sum(len(bars) for bars in published.values())
            if last_round:
                break
            sleep(every)

        if polls_left is not None:
            polls_left -= 1

    return total


@dataclass(frozen=True, slots=True)
class _Rescue:
    """What `run_on_demand` watches a rescale with (14/10) — all of it `None` but the set when no
    `on_rescale` was given, which leaves the loop as it was."""

    memory: Remembered | None
    tail: Callable[[Subscription], Sequence[Candle]] | None
    on_rescale: OnRescale | None
    rescaled: set[Subscription]


def _start_watching(  # noqa: PLR0913 — keyword-only; the loop's state, handed over by name
    new: Subscription,
    *,
    source: LiveSource,
    publisher: CandlePublisher,
    seen: dict[Subscription, dt.datetime],
    watching: set[Subscription],
    refused: set[Subscription],
    on_disk: Callable[[Subscription], dt.datetime | None] | None,
    max_catch_up: int,
    rescue: _Rescue,
) -> int:
    """Start watching a pair just asked for: resumed from its stream or the disk and caught up at
    once. Returns how many bars that published."""
    _resume(publisher, (new,), seen)
    stored = None if new in seen or on_disk is None else on_disk(new)
    try:
        source.subscribe(new.symbol)
    except ConnectionError:
        raise
    except Exception:
        logger.exception("%s %s: the terminal refused it", new.symbol, new.timeframe)
        refused.add(new)
        return 0
    watching.add(new)
    logger.info("%s %s: now watched", new.symbol, new.timeframe)
    if rescue.memory is not None and rescue.tail is not None:
        rescue.memory[new] = deque(rescue.tail(new), maxlen=MEMORY)
    if stored is not None:
        seen[new] = stored
        logger.info(
            "%s %s: catching up from the last bar on disk, %s", new.symbol, new.timeframe, stored
        )
    if new not in seen:
        return 0
    # Caught up as far as a first fill may reach, from the disk or from the stream: a machine
    # switched off for a week owes more than an outage does.
    return sum(
        len(bars)
        for bars in poll_once(
            source,
            publisher,
            (new,),
            seen=seen,
            max_backfill=max_catch_up,
            memory=rescue.memory,
            on_rescale=rescue.on_rescale,
            rescaled=rescue.rescaled,
        ).values()
    )


def _forget_rescaled(
    rescaled: set[Subscription],
    watching: set[Subscription],
    seen: dict[Subscription, dt.datetime],
    memory: Remembered | None,
) -> None:
    """Every pair of a symbol whose series was collected again is watched again from scratch: its
    position and its memory are of the old scale, and the next round resumes it from the stream
    and remembers the newest of the stream and the disk — the disk, now, in the new scale."""
    symbols = {pair.symbol for pair in rescaled}
    for pair in [pair for pair in watching if pair.symbol in symbols]:
        watching.discard(pair)
        seen.pop(pair, None)
        if memory is not None:
            memory.pop(pair, None)
        logger.info(
            "%s %s: watched again from scratch after the rescale", pair.symbol, pair.timeframe
        )
    rescaled.clear()


class TickerLiveSource:
    """A live source asked by the broker's ticker for an internal name (ADR-0032): `WIN` is
    watched as `WIN$`. `tickers` is read on every call, so the loop can add pairs as they are
    asked for; a symbol missing from it is asked for by its own name."""

    def __init__(self, source: LiveSource, tickers: Mapping[str, str]) -> None:
        self._source = source
        self._tickers = tickers

    def subscribe(self, symbol: str) -> None:
        """Select the ticker."""
        self._source.subscribe(self._tickers.get(symbol, symbol))

    def recent_closed(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        """The ticker's closed bars; a `Candle` carries no name."""
        return self._source.recent_closed(self._tickers.get(symbol, symbol), timeframe, count)

    def reconnect(self) -> None:
        """Attach the terminal again."""
        self._source.reconnect()
