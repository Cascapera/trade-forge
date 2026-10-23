"""The deal scan: what it publishes, and the four things it refuses to publish.

Like `test_snapshot.py`, the thread is not the subject — `scan_once` is, which is the whole of
what the thread does. What matters is which executions reach a session, which do not, and that a
refusal never leaves the loop stuck on the deal it refused.

⚠️ **`MT5Gateway` itself is not exercised here and must not be**, for the reason its own docstring
gives: a mock of `history_deals_get` proves that this file calls a function this file also
describes. It was verified against a real terminal instead (2026-08-31, account 462485), which is
where the two facts this module depends on were measured — the server clock is UTC+3, and a deal
five days old had no tick within a second of it.
"""

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import redis

from tradeforge_executor.deals import (
    DEALS_PUBLISHED,
    DEALS_WATERMARK,
    DealReader,
    DealWatch,
    Watermark,
)
from tradeforge_executor.gateway import MT5Gateway, VenueDeal, deal_from, spread_from
from tradeforge_executor.wire import WireFill

NOW = dt.datetime(2026, 8, 31, 17, 0, tzinfo=dt.UTC)
SESSION = "3db55fea-59f5-4857-bdb1-92bf8f5503de"


def a_deal(
    *,
    ticket: int = 1,
    client_id: str = "demand-20260730T1500-303",
    at: dt.datetime | None = None,
) -> VenueDeal:
    return VenueDeal(
        ticket=ticket,
        order_ticket=47_151_856,
        client_id=client_id,
        symbol="EURUSD",
        at=at or (NOW - dt.timedelta(seconds=30)),
        price=Decimal("1.15072"),
        volume=Decimal("0.02"),
        entry=0,
    )


class FakeRedis:
    """The two calls `DealWatch` makes. Answers `bytes`, like redis-py does."""

    def __init__(self, *, stored: object = None, writable: bool = True) -> None:
        self.stored = stored
        self.writes: list[object] = []
        self.claimed: set[object] = set()
        self._writable = writable

    def get(self, name: object) -> object:
        assert str(name) == DEALS_WATERMARK
        return self.stored

    def set(self, name: object, value: object) -> object:
        if not self._writable:
            raise ConnectionError("redis went away")
        assert str(name) == DEALS_WATERMARK
        self.stored = str(value).encode()
        self.writes.append(value)
        return True

    def sadd(self, name: str, *values: object) -> object:
        """Redis' own answer: how many members were **new**. Zero means already claimed."""
        assert name == DEALS_PUBLISHED
        fresh = [value for value in values if value not in self.claimed]
        self.claimed.update(fresh)
        return len(fresh)


class FakeVenue:
    def __init__(self, *deals: VenueDeal, spread: Decimal | None = Decimal("0.00003")) -> None:
        self._deals = deals
        self._spread = spread
        self.asked_since: list[dt.datetime] = []

    def deals_since(self, moment: dt.datetime) -> tuple[VenueDeal, ...]:
        self.asked_since.append(moment)
        return self._deals

    def spread_at(self, symbol: str, at: dt.datetime, *, within: dt.timedelta) -> Decimal | None:
        return self._spread


class FakeStream:
    def __init__(self) -> None:
        self.published: list[WireFill] = []

    def publish_outcome(self, outcome: WireFill) -> None:
        self.published.append(outcome)


def watch(  # noqa: PLR0913 — every argument is a seam this file varies
    venue: FakeVenue,
    stream: FakeStream,
    client: FakeRedis,
    *,
    session_of: object = None,
    already_reported: object = None,
    quote_window: dt.timedelta = dt.timedelta(seconds=2),
) -> DealWatch:
    return DealWatch(
        client,
        venue,
        stream,
        session_of=session_of or (lambda _client_id: SESSION),  # type: ignore[arg-type]
        already_reported=already_reported or (lambda _ticket: False),  # type: ignore[arg-type]
        every=dt.timedelta(seconds=10),
        quote_window=quote_window,
        now=lambda: NOW,
    )


def test_the_gateway_answers_the_two_questions_this_module_asks() -> None:
    """⚠️ **Proved by assignment, not by claiming it in a docstring.** A `Protocol` nothing is ever
    checked against is a description of an imaginary client — and this one has three ways to go
    wrong at once: a narrowed parameter, a narrowed return, and a keyword that is positional on
    one side. mypy sees this line; it does not see a comment."""
    gateway: DealReader = MT5Gateway()
    assert gateway is not None


def test_a_deal_the_order_loop_never_saw_reaches_the_session() -> None:
    """The whole point. A limit that filled an hour after it was placed has nobody in the order
    loop watching for it, and until this scan existed the session was never told."""
    venue = FakeVenue(a_deal())
    stream = FakeStream()

    published = watch(venue, stream, FakeRedis()).scan_once()

    assert published == 1
    [fill] = stream.published
    assert fill.client_id == "demand-20260730T1500-303"
    assert fill.session_id == SESSION
    assert fill.price == Decimal("1.15072")
    assert fill.volume == Decimal("0.02")
    assert fill.spread == Decimal("0.00003"), "the recovered quote did not reach the wire"


def test_a_deal_that_cannot_be_priced_is_not_published() -> None:
    """⚠️ **The same refusal `Placement.__post_init__` already makes**, on the path where the
    quote has to be recovered instead of read before the send.

    A fill that cannot say what crossing cost is a fill priced against a bid-based bar as though
    it were free, and free is the made-up cost that looks most like a measurement. Measured on a
    real deal: with a two-second window the terminal had nothing to say, because the nearest tick
    was 3 438 ms away.
    """
    venue = FakeVenue(a_deal(), spread=None)
    stream = FakeStream()
    scan = watch(venue, stream, FakeRedis())

    assert scan.scan_once() == 0
    assert stream.published == []
    assert scan.skipped == 1, "a refusal that is not counted looks like an empty market"


def test_a_deal_the_order_loop_already_published_is_not_published_twice() -> None:
    """⚠️ **The failure that would be worse than the bug.** A market order trades inside
    `order_send`, so the order loop publishes its fill on the spot — and the same execution is in
    the history this scan reads. Sent twice, a session opens two positions for one trade."""
    venue = FakeVenue(a_deal(ticket=99))
    stream = FakeStream()
    asked: list[int] = []

    def already(ticket: int) -> bool:
        asked.append(ticket)
        return True

    scan = watch(venue, stream, FakeRedis(), already_reported=already)

    assert scan.scan_once() == 0
    assert stream.published == []
    assert asked == [99], "the deal ticket is what identifies an execution, and it was not asked"


def test_a_deal_that_cannot_be_attributed_is_not_published() -> None:
    """`WireFill.session_id` routes a fill to the strategy that armed the order. Guessing it would
    hand a real position to a session that never asked for one — the ghost with the sign flipped,
    and worse, because the position is real."""
    venue = FakeVenue(a_deal())
    stream = FakeStream()

    scan = watch(venue, stream, FakeRedis(), session_of=lambda _client_id: None)

    assert scan.scan_once() == 0
    assert stream.published == []
    assert scan.skipped == 1


def test_a_refused_deal_still_moves_the_watermark() -> None:
    """⚠️ **The harder half of the refusal, and the one a test has to pin.**

    Leaving the mark behind a deal that cannot be priced would make every later scan find it,
    refuse it, and never reach the deals behind it — one bad execution stopping the loop for ever,
    which is the original bug wearing the cure's clothes. Nothing about it will be different next
    time: the tick history it was priced against does not grow back.
    """
    stuck = a_deal(ticket=1, at=NOW - dt.timedelta(minutes=5))
    later = a_deal(ticket=2, at=NOW - dt.timedelta(minutes=4))
    client = FakeRedis()

    watch(FakeVenue(stuck, later, spread=None), FakeStream(), client).scan_once()

    assert client.writes, "the watermark never moved; the next scan repeats this one"
    assert client.stored == str(int(later.at.timestamp() * 1000)).encode(), (
        "the mark stopped at the first refused deal instead of passing both"
    )


def test_the_watermark_is_read_back_from_the_bytes_redis_returns() -> None:
    """⚠️ **redis-py answers `bytes` unless the client decodes**, and this module is handed
    whichever client the process has. A branch that only understood `str` would pass every test
    written against a fake and start from `now` against the real thing — forgetting, on every
    scan, in the one place whose whole job is to remember across restarts."""
    deal = a_deal(at=NOW - dt.timedelta(minutes=1))
    stored = int((NOW - dt.timedelta(hours=2)).timestamp() * 1000)
    venue = FakeVenue(deal)

    watch(venue, FakeStream(), FakeRedis(stored=str(stored).encode())).scan_once()

    [asked] = venue.asked_since
    assert asked == dt.datetime.fromtimestamp((stored + 1) / 1000, dt.UTC), (
        "the stored mark was not understood, so the scan asked from the wrong instant"
    )


def test_a_watermark_that_is_not_an_instant_starts_from_now() -> None:
    """⚠️ **Unreadable is treated as absent, never as zero.** A mark of zero would ask the terminal
    for every deal since 1970 and republish an account's whole history to whatever session happens
    to be running. Both are "I cannot tell"; only one of them is safe."""
    venue = FakeVenue()

    watch(venue, FakeStream(), FakeRedis(stored=b"not-an-instant")).scan_once()

    assert venue.asked_since == [NOW]


def test_the_scan_starts_from_now_when_nothing_is_stored() -> None:
    """A first-ever run faces a history it has never seen. Replaying it would publish every deal
    the account has ever done to sessions that are long gone."""
    venue = FakeVenue()

    watch(venue, FakeStream(), FakeRedis(stored=None)).scan_once()

    assert venue.asked_since == [NOW]


def test_the_mark_is_asked_for_one_millisecond_past_the_last_deal() -> None:
    """⚠️ `history_deals_get` includes its start instant, so asking from the mark itself returns
    the deal that set it — for ever, on every scan, republished each time it is not caught by the
    duplicate check."""
    deal = a_deal(at=NOW - dt.timedelta(minutes=1))
    client = FakeRedis()
    venue = FakeVenue(deal)
    scan = watch(venue, FakeStream(), client)

    scan.scan_once()
    scan.scan_once()

    assert venue.asked_since[1] > deal.at, "the second scan would find the first scan's deal again"


def test_a_venue_that_cannot_be_read_leaves_the_watermark_alone() -> None:
    """ "I could not ask" and "there was nothing" are different facts. Advancing the mark on a
    failed read would skip whatever executed during the outage — silently, and precisely when the
    executor was least able to notice.

    ⚠️ **The early `return 0` is equivalent to falling through with no deals, and measured to be.**
    A mutant replacing it with `deals = ()` survives this test and every other one, because a loop
    over nothing does nothing: same watermark, same count, same silence. It is written as a return
    for the reader — the two facts above stay different even where the behaviour does not — and
    not as a guard anything can observe. Recorded so a green suite is not read as proof of the
    distinction.
    """

    class Unreadable(FakeVenue):
        def deals_since(self, moment: dt.datetime) -> tuple[VenueDeal, ...]:
            raise ConnectionError("the terminal went away")

    client = FakeRedis()

    assert watch(Unreadable(), FakeStream(), client).scan_once() == 0
    assert client.writes == []


def test_a_watermark_that_cannot_be_written_does_not_stop_the_scan() -> None:
    """⚠️ Logged and survived rather than raised. A mark that failed to move means the next scan
    finds this deal again — a duplicate, which the audit check then catches — but raising here
    would stop the scan and leave every deal behind it unnoticed, which is the original bug."""
    stream = FakeStream()

    published = watch(
        FakeVenue(a_deal(ticket=1), a_deal(ticket=2)),
        stream,
        FakeRedis(writable=False),
    ).scan_once()

    assert published == 2, "the second deal was lost when the first mark failed to write"


def test_the_redis_client_answers_the_two_calls_this_module_makes() -> None:
    """The same proof-by-assignment as the gateway above, for the other seam."""
    client: Watermark = redis.Redis()
    assert client is not None


# --------------------------------------------------------------------------- #
# Shaping one record of the venue's history — the decisions the gateway defers  #
# --------------------------------------------------------------------------- #


class RawDeal(SimpleNamespace):
    """One row of `history_deals_get`, in the shape the terminal really returns.

    ⚠️ **Not a mock of MT5 — the fields were read off a real deal** on 2026-08-31 (account
    462485, deal 35176079): `ticket, order, symbol, volume, price, time, time_msc, magic,
    comment, entry, position_id, profit, ...`. A shape anchored to a measurement, which is the
    difference between a fixture and a fiction.
    """


def a_raw(**overrides: object) -> RawDeal:
    values: dict[str, object] = {
        "ticket": 35_176_079,
        "order": 47_096_513,
        "symbol": "EURUSD",
        "volume": 0.01,
        "price": 1.16524,
        # 2026-08-26 20:52:36 in the **server's** clock, which is what MT5 hands out.
        "time_msc": int(dt.datetime(2026, 8, 26, 20, 52, 36, tzinfo=dt.UTC).timestamp() * 1000),
        "magic": 770_302,
        "comment": "demand-20260730T1500-303",
        "entry": 0,
    }
    values.update(overrides)
    return RawDeal(**values)


def test_the_servers_clock_is_corrected_to_utc() -> None:
    """⚠️ **The failure this prevents is silent, and it disables the whole feature.**

    MT5 hands out server time labelled as UTC. Measured 2026-08-31: `symbol_info_tick().time`
    read as UTC said `20:49` while UTC was `17:49` — exactly the three hours below. Left
    uncorrected, every deal is stamped three hours into the future, so the quote lookup searches
    three hours away from the trade, finds nothing, and the executor reports a thin market
    instead of a bug.
    """
    deal = deal_from(a_raw(), server_offset=dt.timedelta(hours=3))

    assert deal is not None
    assert deal.at == dt.datetime(2026, 8, 26, 17, 52, 36, tzinfo=dt.UTC), (
        "the deal is stamped in the terminal's clock rather than in UTC"
    )


def test_the_correction_is_the_offset_it_was_given() -> None:
    """⚠️ Two offsets, because one would pass against a function that ignored the argument and
    subtracted a constant — and a constant is what a broker changes at daylight saving."""
    at_three = deal_from(a_raw(), server_offset=dt.timedelta(hours=3))
    at_two = deal_from(a_raw(), server_offset=dt.timedelta(hours=2))

    assert at_three is not None
    assert at_two is not None
    assert at_two.at - at_three.at == dt.timedelta(hours=1)


def test_a_deal_with_no_name_is_not_shaped_at_all() -> None:
    """The comment is the only field of ours that survives into the venue's own history. A deal
    without one cannot be correlated, and correlating it by proximity would tell a session that a
    region it never armed had traded."""
    assert deal_from(a_raw(comment=""), server_offset=dt.timedelta(hours=3)) is None
    assert deal_from(a_raw(comment="   "), server_offset=dt.timedelta(hours=3)) is None


def test_the_name_is_read_off_the_comment() -> None:
    deal = deal_from(a_raw(comment=" zone-7 "), server_offset=dt.timedelta(0))

    assert deal is not None
    assert deal.client_id == "zone-7", "the venue pads comments; the name did not survive"


def test_prices_cross_as_decimals_of_the_text_not_of_the_float() -> None:
    """⚠️ `Decimal(str(x))`, never `Decimal(float)`. The venue quotes 1.16524 and the binary
    double nearest it is not that number — and this price is what a session's ledger is built
    from."""
    deal = deal_from(a_raw(price=1.16524), server_offset=dt.timedelta(0))

    assert deal is not None
    assert deal.price == Decimal("1.16524")


# --------------------------------------------------------------------------- #
# The four the guardian found: duplication, a dead thread, permanent loss, order #
# --------------------------------------------------------------------------- #


def test_a_deal_is_claimed_once_even_when_the_watermark_cannot_be_written() -> None:
    """⚠️ **The duplication the audit check does not catch, and the one that reaches a ledger.**

    `order_audit` only knows what the *order loop* published — a resting limit records `deal=0` —
    so every fill this scan discovers is unknown to it. Measured on the first draft: three scans
    against a Redis that could not `SET` published one deal **three times**, and
    `MT5Broker._request_for` reads its map without popping, so all three reach `Portfolio.apply`.
    Three positions in the ledger for one at the venue.
    """
    deal = a_deal(ticket=35_176_079)
    client = FakeRedis(writable=False)
    stream = FakeStream()

    for _ in range(3):
        watch(FakeVenue(deal), stream, client).scan_once()

    assert len(stream.published) == 1, (
        f"the same execution reached the session {len(stream.published)} times"
    )


def test_the_claim_is_taken_before_the_fill_is_published() -> None:
    """⚠️ Order matters, and it decides which way a crash breaks. Claiming first means a process
    that dies between the two loses a fill; publishing first means it duplicates one. A missing
    fill is what the next scan and an operator can still see; a double-counted position is not
    distinguishable from a real second execution."""
    seen: list[str] = []

    class Watching(FakeRedis):
        def sadd(self, name: str, *values: object) -> object:
            seen.append("claim")
            return super().sadd(name, *values)

    class Noting(FakeStream):
        def publish_outcome(self, outcome: WireFill) -> None:
            seen.append("publish")
            super().publish_outcome(outcome)

    watch(FakeVenue(a_deal()), Noting(), Watching()).scan_once()

    assert seen == ["claim", "publish"]


def test_the_scan_survives_a_failure_while_pricing_a_deal() -> None:
    """⚠️ **The thread must not be able to die.** An exception escaping the loop kills it
    silently, `stop()` joins a corpse without complaining, and the executor goes on taking orders
    while no filled limit reaches a session again — this PR's own bug, hidden behind a feature
    that looks present."""

    class Flaky(FakeVenue):
        def spread_at(
            self, symbol: str, at: dt.datetime, *, within: dt.timedelta
        ) -> Decimal | None:
            raise ConnectionError("the terminal went away between the two reads")

    scan = watch(Flaky(a_deal()), FakeStream(), FakeRedis())

    assert scan.scan_once() == 0, "an exception escaped and would have killed the thread"


def test_a_deal_nobody_could_decide_on_is_left_where_it_is() -> None:
    """⚠️ **A transient failure must not become a permanent loss.**

    `docker compose restart postgres` at 09:15:02; a limit fills at 09:15:04; the 09:15:10 scan
    asks the database, cannot reach it, and — in the first draft — skipped the deal *and* moved
    the mark past it. The database comes back and the deal is behind the watermark for ever: a
    real position the session will never hear about, which is exactly the ghost this module
    exists to kill, delivered by its own error handling.
    """
    deal = a_deal()
    client = FakeRedis()

    def cannot_ask(_client_id: str) -> str | None:
        raise ConnectionError("the database went away")

    scan = watch(FakeVenue(deal), FakeStream(), client, session_of=cannot_ask)

    assert scan.scan_once() == 0
    assert client.writes == [], "the mark moved past a deal nobody managed to look at"


def test_an_undecided_deal_stops_the_scan_rather_than_being_stepped_over() -> None:
    """⚠️ The deals are in execution order and the mark is a high-water mark. Publishing the ones
    behind an undecided deal would strand it: the mark would move past it on their account."""
    first = a_deal(ticket=1, at=NOW - dt.timedelta(minutes=2))
    second = a_deal(ticket=2, at=NOW - dt.timedelta(minutes=1))
    stream = FakeStream()

    def only_the_second_is_answerable(_client_id: str) -> str | None:
        raise ConnectionError("the database went away")

    watch(
        FakeVenue(first, second),
        stream,
        FakeRedis(),
        session_of=only_the_second_is_answerable,
    ).scan_once()

    assert stream.published == []


def test_deals_are_published_oldest_first_whatever_order_they_arrive_in() -> None:
    """⚠️ **The mark is written from the last deal of the loop**, so a reversed order leaves it on
    the *oldest* — and the next scan finds and republishes everything newer. A mutant reversing
    the sort inside `MT5Gateway` survived the whole suite, because that class has no unit tests by
    policy; the sort belongs where the thing that depends on it lives."""
    old = a_deal(ticket=1, at=NOW - dt.timedelta(minutes=9))
    new = a_deal(ticket=2, at=NOW - dt.timedelta(minutes=1))
    client = FakeRedis()
    stream = FakeStream()

    watch(FakeVenue(new, old), stream, client).scan_once()

    assert [fill.at for fill in stream.published] == [old.at, new.at]
    assert client.stored == str(int(new.at.timestamp() * 1000)).encode(), (
        "the watermark ended on the older deal; the next scan repeats the newer one"
    )


def test_a_deal_that_closed_a_position_is_not_reported_as_an_opening() -> None:
    """⚠️ **`entry` is read, and this is the guard it exists for.**

    `MT5Broker` rebuilds a `Fill` from the order it remembers *sending*, so an exit published
    through this channel would have the session open a position in its ledger at the instant the
    venue closed one. Measured on this account: a closing deal comes back `entry=1` and carries a
    comment, so the shape does reach here — the broker's own `[sl ...]` comment is not the only
    thing standing between us and it.
    """
    closing = VenueDeal(
        ticket=35_176_080,
        order_ticket=47_096_514,
        client_id="demand-20260730T1500-303",
        symbol="EURUSD",
        at=NOW - dt.timedelta(seconds=30),
        price=Decimal("1.16512"),
        volume=Decimal("0.01"),
        entry=1,
    )
    stream = FakeStream()

    watch(FakeVenue(closing), stream, FakeRedis()).scan_once()

    assert stream.published == []


# --------------------------------------------------------------------------- #
# Pricing the crossing: which tick, and whether it prices anything at all       #
# --------------------------------------------------------------------------- #


def a_tick(*, at_ms: int, bid: float = 1.16518, ask: float = 1.16519) -> dict[str, object]:
    """One row of `copy_ticks_range`, in the shape the terminal returns.

    ⚠️ Anchored to a measurement: the fields and the values are from the tick that priced deal
    35176079 on this account (bid 1.16518 / ask 1.16519, 1e-05 apart).
    """
    return {"time_msc": at_ms, "bid": bid, "ask": ask}


def test_the_quote_comes_from_the_tick_nearest_the_deal() -> None:
    """⚠️ **The plausible neighbour is the *furthest* tick, not an absent one.** It answers with a
    real quote from a real tick — just the wrong one — and the further it is, the more wrong the
    cost it charges."""
    spread = spread_from(
        [
            a_tick(at_ms=1_000, bid=1.0, ask=1.9),
            a_tick(at_ms=5_050, bid=1.16518, ask=1.16519),
            a_tick(at_ms=9_000, bid=2.0, ask=2.9),
        ],
        5_000,
    )

    assert spread == Decimal("0.00001"), "a tick four seconds away priced the crossing"


def test_a_tick_either_side_of_the_deal_is_a_candidate() -> None:
    """Nearest by absolute distance: the tick *before* a deal is often the better answer, and a
    search that only looked forward would take a worse one or none at all."""
    before = spread_from([a_tick(at_ms=4_900, bid=1.1, ask=1.2)], 5_000)

    assert before == Decimal("0.1").quantize(Decimal("0.1"))


def test_a_tick_with_no_book_does_not_price_a_crossing() -> None:
    """⚠️ **`bid == ask == 0` subtracts to a clean zero, and nothing downstream refuses that.**

    MT5 publishes trade-only ticks with no quote attached. A negative spread is caught one layer
    down by `Placement` — "a spread is a magnitude" — but zero sails through as *free execution,
    published as a measurement*, which is the made-up cost this project has a rule about.
    """
    assert spread_from([a_tick(at_ms=5_000, bid=0.0, ask=0.0)], 5_000) is None
    assert spread_from([a_tick(at_ms=5_000, bid=1.16518, ask=0.0)], 5_000) is None
    assert spread_from([a_tick(at_ms=5_000, bid=0.0, ask=1.16519)], 5_000) is None


def test_no_ticks_at_all_prices_nothing() -> None:
    """The measured case: `±1s` around a real deal returned nothing."""
    assert spread_from([], 5_000) is None
    assert spread_from(None, 5_000) is None


def test_a_deal_left_by_a_transient_failure_is_published_on_the_next_scan() -> None:
    """⚠️ **The half of the invariant the first version of this file assumed.**

    `test_a_deal_nobody_could_decide_on_is_left_where_it_is` proves the watermark does not move.
    It does not prove the deal is ever seen *again*, and for one round of this PR it was not: the
    claim was taken before the two Postgres lookups, so a database restart between them left the
    ticket claimed and the deal undecided — and the next scan found it claimed and refused it for
    ever. "Not lost now" and "recovered later" are two facts, and only the second is the one this
    module exists to deliver.

    The scenario is the ordinary one: `docker compose restart postgres` at 09:15:02, a limit fills
    at 09:15:04, the 09:15:10 scan cannot ask, the database is back by 09:15:15.
    """
    deal = a_deal(ticket=999)
    client = FakeRedis()
    stream = FakeStream()
    asked = 0

    def down_the_first_time(_client_id: str) -> str | None:
        nonlocal asked
        asked += 1
        if asked == 1:
            raise ConnectionError("the database is restarting")
        return SESSION

    first = watch(FakeVenue(deal), stream, client, session_of=down_the_first_time).scan_once()
    second = watch(FakeVenue(deal), stream, client, session_of=down_the_first_time).scan_once()

    assert first == 0, "the scenario did not exercise a transient failure"
    assert second == 1, (
        "the deal was claimed by the scan that could not finish, and is now refused for ever"
    )
    assert len(stream.published) == 1
