"""Noticing that a resting order finally traded, which nobody in the order loop can.

**The gap this closes.** The executor treats one queue entry as one decision: take the order,
send it, record it, publish the fill if there was one, ack. That closes correctly for a market
order, where the trade happens inside the call. For a **limit** it does not. The placement comes
back `retcode=10009` with `deal=0` — accepted and resting — and the execution happens minutes or
hours later, by which time the entry has been acknowledged and the executor has forgotten the
order. A session that arms limits, which is what the SMC strategy does (ADR-0014), opens a
position at the venue and is **never told**. The ledger and the account then diverge in silence,
which is the worst failure mode there is for real money.

⚠️ **And the damage is not only in the money.** ADR-0015's rule is that a region is spent by the
**fill**, not by the placement — so a fill nobody hears about is a zone that never burns, and the
strategy goes on offering a region it has already traded. The ledger diverging is what an operator
would eventually see on a statement; this one is invisible from outside and changes what the
strategy decides next.

**Why a thread, and not a line in the order loop.** `snapshot.py` made this argument first and it
applies here unchanged: *the cadence of a signal must not be borrowed from the cadence of the
work*. A fill happens on the market's schedule; the order loop turns when a strategy speaks. A
limit armed on Monday and filled on Wednesday would be noticed on whichever later bar happened to
produce another order — or never.

**Two mechanisms, and they do different jobs.** The order loop already publishes a `WireFill`
for a deal that traded inside `order_send`, and that same execution is in the history this loop
reads. Publishing it twice would have a session open two positions for one trade — strictly worse
than the bug being fixed. What prevents it is `order_audit`, where the order loop recorded the
deal ticket before acking (`ledger.deal_was_reported`): a durable record of what was already
said. The **watermark** is not that proof; it only bounds how far back each scan asks, so a
ten-second loop does not re-read a month of history.

⚠️ **The watermark was the de-duplication in the first draft of this module, and it was wrong.**
The order loop cannot advance it honestly — `order_send` answers with a ticket and no instant, so
the loop does not know *where* to move a mark measured in deal time — and a process dying between
publishing and advancing would leave exactly the duplicate it was supposed to prevent. Correctness
belongs on the durable record; cost control belongs on the mark.

⚠️ **What the watermark still cannot do.** It is a high-water mark in deal time, so an execution
that lands in history *behind* one already scanned is not looked at again. Against one terminal
deals are appended in execution order and this does not arise; it is written down because the day
it does, the symptom is a missing fill rather than a duplicated one, which is the direction this
trades towards deliberately.
"""

import datetime as dt
import logging
import threading
from collections.abc import Callable
from decimal import Decimal
from enum import Enum, auto
from typing import Protocol

from redis.typing import EncodableT, KeyT

from tradeforge_executor.gateway import VenueDeal
from tradeforge_executor.wire import WireFill

logger = logging.getLogger(__name__)

__all__ = ["DEALS_PUBLISHED", "DEALS_WATERMARK", "DealReader", "DealWatch", "Watermark"]

_DEAL_ENTRY_IN = 0
"""MT5's `DEAL_ENTRY_IN`: this execution opened a position. The only kind this scan reports."""


class _Verdict(Enum):
    """What one deal earned. Only the first two let the watermark past them."""

    PUBLISHED = auto()

    REFUSED = auto()
    """Not published, and it never will be. The answer does not change with time."""

    UNDECIDED = auto()
    """This scan could not tell. Looked at again next time, and the mark stays behind it."""


DEALS_WATERMARK = "venue.deals.watermark"
"""Where the executor remembers the last deal it published, as milliseconds since the epoch.

⚠️ **In Redis rather than in memory, because the case it exists for is a restart.** An executor
that came back with an empty memory would either republish everything history still holds, or
start from *now* and lose whatever executed while it was down — and the second is the failure
this module exists to prevent, arriving through its own cure.

⚠️ **No TTL, deliberately**, unlike `VENUE_STATE`. A snapshot that expires reads as "I do not
know", which is the right answer for a fact about the present. This is a fact about the past, and
it does not become less true by being old — an expired watermark would restart the scan from
scratch and republish deals a session has already acted on.
"""


DEALS_PUBLISHED = "venue.deals.published"
"""The deal tickets this scan has claimed, as a Redis set.

⚠️ **Durable and without a TTL, for the same reason as the watermark**: it is a fact about
the past. A set that expired would let an old deal reach whatever session is running then —
which, unlike a missing fill, cannot be told apart from a real second execution.

⚠️ It grows without bound, slowly: one small integer per execution this system discovers. An
account doing a hundred trades a day adds about 36 000 members a year, which Redis holds in
well under a megabyte. Trimming it is a decision for the day it matters, and trimming it
wrongly republishes fills.
"""


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _as_millis(stored: object) -> int | None:
    """The watermark as an integer, or `None` for anything that is not one.

    ⚠️ **`bytes` first, and it is not defensiveness.** redis-py answers `bytes` unless the client
    was built with `decode_responses=True`, and this module is handed whichever client the
    process happens to have. A branch that only handled `str` would work in every test written
    against a fake and return `None` against the real thing — the `fake que diverge` failure, in
    the one place whose whole job is remembering across restarts.
    """
    if isinstance(stored, bytes):
        try:
            stored = stored.decode()
        except UnicodeDecodeError:
            return None
    if isinstance(stored, int) and not isinstance(stored, bool):
        return stored
    if isinstance(stored, str):
        try:
            return int(stored)
        except ValueError:
            return None
    return None


class DealReader(Protocol):
    """The two questions this module asks the venue, and no more.

    ⚠️ **Narrower than `OrderGateway` on purpose** — the same argument `PositionReader` makes in
    `snapshot.py`. A `Protocol` describes what the *caller* needs, so a double written against it
    is a small honest object rather than a mock of the whole gateway. `MT5Gateway` satisfies it
    structurally, and `test_deals.py` proves that by assignment rather than by claiming it.
    """

    def deals_since(self, moment: dt.datetime) -> tuple[VenueDeal, ...]: ...

    def spread_at(
        self, symbol: str, at: dt.datetime, *, within: dt.timedelta
    ) -> Decimal | None: ...


class Watermark(Protocol):
    """The two Redis calls this module makes, spelled in redis-py's own vocabulary.

    The names and types are the library's: renaming `name` or narrowing it describes a client
    that does not exist, and every double written against it would type-check while the real
    thing did not.
    """

    def get(self, name: KeyT) -> object: ...

    def set(self, name: KeyT, value: EncodableT) -> object: ...

    # ⚠️ `name: str`, not `KeyT`, and mypy is why this is right: redis-py declares `sadd`
    # narrower than `get`/`set`. A protocol wider than the client describes a client that
    # does not exist, and every double written against it would type-check while the real
    # thing did not.
    def sadd(self, name: str, *values: EncodableT) -> object: ...


class OutcomePublisher(Protocol):
    """Whoever puts an outcome on `venue.outcomes`. `Service` satisfies it."""

    def publish_outcome(self, outcome: WireFill) -> None: ...


class DealWatch:
    """Publishes executions the order loop never saw, every `every`, until stopped."""

    def __init__(  # noqa: PLR0913 — a gateway, a stream, a clock and the two windows
        self,
        client: Watermark,
        gateway: DealReader,
        publisher: OutcomePublisher,
        *,
        session_of: Callable[[str], str | None],
        already_reported: Callable[[int], bool],
        every: dt.timedelta,
        quote_window: dt.timedelta,
        now: Callable[[], dt.datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._gateway = gateway
        self._publisher = publisher
        self._already_reported = already_reported
        """Whether the order loop already published this deal. See `ledger.deal_was_reported`.

        ⚠️ Asked of the database rather than remembered here, because the run that published it
        may not be this one — a restarted executor with an empty memory would republish every
        deal still in history to whatever session is running now.
        """

        self._session_of = session_of
        """Which live session owns a `client_id`, or `None` if this executor cannot say.

        ⚠️ A deal we cannot attribute is **not** published. `WireFill.session_id` is what routes
        the fill to the session that armed the order, and a wrong one would hand a real position
        to a strategy that never asked for it. Skipping is recoverable by a human reading the
        log; misrouting a position is not.
        """

        self._every = every
        self._quote_window = quote_window
        self._now = now
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self.published = 0
        self.skipped = 0
        """Deals seen and deliberately not published — unpriceable, or unattributable. Counted
        rather than only logged, because "the scan is running and publishing nothing" and "the
        scan is running and refusing everything it finds" look identical from outside."""

    # ----------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Scan once, immediately, then on the interval.

        ⚠️ **The first scan is at `start()`.** The window this module exists to close is widest
        exactly at start-up: whatever executed while the executor was down is sitting in history
        right now, and waiting one interval to look would add that interval to an outage that has
        already happened.
        """
        self.scan_once()
        thread = threading.Thread(target=self._loop, name="venue-deals", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.wait(self._every.total_seconds()):
            self.scan_once()

    def __enter__(self) -> "DealWatch":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # --------------------------------------------------------------------- work
    def scan_once(self) -> int:
        """One pass over what the venue executed since the watermark. Returns what it published.

        ⚠️ **Three outcomes per deal, not two, and the third is what the first draft got wrong.**
        A deal is published, *refused*, or *undecided* — and only the first two advance the
        watermark. Refused means the answer will not change: the tick history a deal was priced
        against does not grow back, so retrying for ever would leave one bad execution stopping
        the loop and every deal behind it unseen. **Undecided** means this scan could not ask —
        the database was down — and moving the mark past it turns a database restart into a
        position the session will never hear about. That is the ghost this module exists to kill,
        delivered by its own error handling.

        ⚠️ **An undecided deal stops the scan there** rather than being stepped over. The deals
        are in execution order and the mark is a high-water mark: going on would strand it behind
        whatever was published after it.

        ⚠️ **Sorted here, not in the gateway.** The order is what the mark depends on, so it
        belongs beside the code that depends on it — and a mutant reversing it inside `MT5Gateway`
        survived the whole suite, because that class has no unit tests by policy.
        """
        since = self._since()
        try:
            found = self._gateway.deals_since(since)
        except Exception:
            logger.exception("could not read the venue's history; the watermark stays put")
            return 0

        published = 0
        for deal in sorted(found, key=lambda deal: (deal.at, deal.ticket)):
            verdict = self._consider(deal)
            if verdict is _Verdict.UNDECIDED:
                logger.warning(
                    "stopping this scan at deal %s; it will be looked at again", deal.ticket
                )
                break
            if verdict is _Verdict.PUBLISHED:
                published += 1
            else:
                self.skipped += 1
            self._remember(deal)
        return published

    def _consider(self, deal: VenueDeal) -> "_Verdict":
        """One deal: published, refused for good, or left for the next scan.

        ⚠️ **Every failure in here is caught, because the thread must not be able to die.** The
        loop above runs on a `threading.Thread`; an exception escaping it kills that thread
        silently, `stop()` joins a corpse without complaining, and the executor goes on taking
        orders while no filled limit ever reaches a session again — this PR's own bug, hidden
        behind a feature that looks present. `VenueSnapshot.publish_once` wraps both of its halves
        for this reason, and the first draft of this file wrapped only the history read.

        An unexpected failure is **undecided**, not refused: this code cannot tell whether the
        next attempt would go differently, and the safe guess is the one that looks again.
        """
        try:
            return self._verdict_for(deal)
        except Exception:
            logger.exception("could not decide what to do with deal %s; leaving it", deal.ticket)
            return _Verdict.UNDECIDED

    def _verdict_for(self, deal: VenueDeal) -> "_Verdict":
        # ⚠️ **`entry` is read, and this is the guard it exists for.** A deal that *closed* a
        # position is not an opening fill: `MT5Broker` rebuilds a `Fill` from the order it
        # remembers sending, so publishing an exit would have the session open a position in its
        # ledger at the instant the venue closed one. Measured on this account: a close comes back
        # `entry=1` and carries a comment, so the shape does reach here.
        if deal.entry != _DEAL_ENTRY_IN:
            logger.info(
                "deal %s closed or reversed a position (entry=%s); this scan reports openings",
                deal.ticket,
                deal.entry,
            )
            return _Verdict.REFUSED

        if self._already_reported(deal.ticket):
            logger.debug("deal %s was already published by the order loop", deal.ticket)
            return _Verdict.REFUSED

        session_id = self._session_of(deal.client_id)
        if session_id is None:
            logger.warning(
                "deal %s names %s, which this executor cannot attribute to a session; "
                "not published",
                deal.ticket,
                deal.client_id,
            )
            return _Verdict.REFUSED

        spread = self._gateway.spread_at(deal.symbol, deal.at, within=self._quote_window)
        if spread is None:
            # ⚠️ **The refusal `Placement.__post_init__` already makes**, on the path where the
            # quote has to be recovered instead of read. A fill that cannot say what crossing cost
            # is a fill priced against a bid-based bar as though it were free — and free is the
            # made-up cost that looks most like a measurement.
            logger.error(
                "deal %s (%s) has no quote within %s of %s; not published, and the session that "
                "armed it will not learn of this position from here",
                deal.ticket,
                deal.client_id,
                self._quote_window,
                deal.at,
            )
            return _Verdict.REFUSED

        # ⚠️ **Claimed here, as late as possible, and atomically.** `SADD` answers 0 when the
        # ticket was already there, so check and claim are one round trip and two scans cannot
        # both publish one deal.
        #
        # ⚠️ **Every question that can fail transiently is already answered by this line**, and
        # that ordering is the whole of a bug this had. Claiming *first* — which is where it was
        # — put two Postgres round trips inside the claimed window: a `docker compose restart
        # postgres` between them left the ticket claimed and the deal undecided, and the next
        # scan found it claimed and refused it **for ever**. The ghost, reintroduced by its own
        # cure. What remains inside the window is `publish_outcome` itself, which is the named
        # trade-off: a crash there loses a fill rather than duplicating one, and a lost fill is
        # something the log and an operator can still see.
        if not self._claim(deal.ticket):
            logger.debug("deal %s was already published", deal.ticket)
            return _Verdict.REFUSED

        self._publisher.publish_outcome(
            WireFill(
                client_id=deal.client_id,
                session_id=session_id,
                symbol=deal.symbol,
                at=deal.at,
                price=deal.price,
                volume=deal.volume,
                spread=spread,
                ticket=deal.order_ticket,
            )
        )
        self.published += 1
        logger.info(
            "deal %s filled %s of %s at %s; published to the session that armed it",
            deal.ticket,
            deal.volume,
            deal.client_id,
            deal.price,
        )
        return _Verdict.PUBLISHED

    def _claim(self, ticket: int) -> bool:
        """Record that this deal is ours to publish. `False` if it was already claimed.

        ⚠️ **The durable half of the de-duplication, and the first draft did not have it.**
        `order_audit` catches only what the *order loop* published, and a resting limit records
        `deal=0` — so every fill this scan discovers is unknown to it and answers `False` for
        ever. That left the watermark as the only guard, and `_remember` swallows a write failure
        by design. Measured: three scans against a Redis that could not `SET` published one deal
        **three times**, and `MT5Broker._request_for` reads its map without popping, so all three
        reach `Portfolio.apply` — three positions in the ledger for one at the venue.
        """
        return bool(self._client.sadd(DEALS_PUBLISHED, ticket))

    # --------------------------------------------------------------- watermark
    def _since(self) -> dt.datetime:
        """Where this scan starts. The stored mark, or **now** when there is none.

        ⚠️ **`now` and not the epoch, and this is the one place the trade-off is visible.** A
        first-ever run with no mark faces a history it has never seen: replaying it would publish
        every deal the account has ever done to sessions that are long gone. Starting at `now`
        means a first run notices nothing that already happened — which is right, because there
        is no running session those deals could belong to, and `_session_of` would refuse them
        anyway.
        """
        try:
            stored = self._client.get(DEALS_WATERMARK)
        except Exception:
            logger.exception("could not read the deal watermark; this scan starts from now")
            return self._now()
        millis = _as_millis(stored)
        if millis is None:
            # ⚠️ Unreadable is treated as absent, **not** as zero. A mark of zero would ask the
            # terminal for every deal since 1970 and republish an account's whole history to
            # whatever session happens to be running. Both are "I cannot tell", and only one of
            # them is safe.
            if stored is not None:
                logger.error(
                    "the deal watermark reads %r, which is not an instant; starting from now",
                    stored,
                )
            return self._now()
        # One millisecond past the last published deal: `history_deals_get` is inclusive of its
        # start, and asking from the mark itself returns that deal for ever.
        return dt.datetime.fromtimestamp((millis + 1) / 1000, dt.UTC)

    def _remember(self, deal: VenueDeal) -> None:
        try:
            self._client.set(DEALS_WATERMARK, int(deal.at.timestamp() * 1000))
        except Exception:
            # ⚠️ Logged and survived rather than raised. A watermark that failed to move means the
            # next scan finds this deal again — a duplicate, which is bad — but raising here would
            # stop the scan and leave every deal behind it unnoticed, which is the original bug.
            logger.exception("could not advance the deal watermark past %s", deal.ticket)
