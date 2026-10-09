"""`/symbols` — what the broker offers, as opposed to what this system has catalogued.

⚠️ **Nothing here talks to MetaTrader, and nothing here ever will.** This process runs in a
Linux container and the MT5 wheel does not exist for Linux (ADR-02, ADR-0021). Every answer
below is served from the `broker_symbols` snapshot that the host agent wrote; the only thing
this router can do about a stale snapshot is *ask* for a new one, by dropping a job on a queue
no container drains.

That is why search is a read of Postgres and not a round trip to the terminal. Measured on
19/08/2026: `symbols_get(group="A*")` costs the terminal **0.1 ms**, so a live query would be
paying queue latency — half a second or more, per keystroke — for a question the database
answers in microseconds, and would stop working entirely whenever the terminal is closed,
which is the normal state of a machine somebody is building a strategy on.
"""

from collections import Counter
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import PROBE_HISTORY, SYNC_SYMBOLS, collect_queue_for
from tradeforge_api.schemas import (
    BrowsedSymbolOut,
    EnqueuedOut,
    MarketOut,
    MarketsOut,
    StorableText,
    Symbol,
    SymbolBrowseOut,
    SymbolHistoryOut,
    SymbolSearchOut,
    SymbolSnapshotOut,
    Timeframe,
)
from tradeforge_db.broker_symbols import (
    DEFAULT_LIMIT,
    MARKETS,
    browse_symbols,
    search_symbols,
    snapshot_taken_at,
)
from tradeforge_db.brokers import broker_slugs, collect_queue
from tradeforge_db.symbol_history import read_history

router = APIRouter(tags=["symbols"])

# A search is one keystroke's worth of work, so the ceiling is a UI decision rather than a
# safety one — but it is still a ceiling, because a query parameter without a maximum is how
# this project already learned that `offset` can be handed the 500 of a bigint.
_MAX_LIMIT = 100


@router.get("/symbols/search", response_model=SymbolSearchOut)
def search(
    session: SessionDep,
    # ⚠️ `StorableText`, not `str`. This parameter goes straight into an `ILIKE` against a text
    # column, and Postgres cannot hold a NUL byte — `?q=%00` raises `DataError` from inside the
    # driver and turns a value the client fully controls into a 500. Found by schemathesis on
    # this very endpoint, which had been green on the previous commit purely because the fuzzer
    # had not drawn that value yet.
    #
    # The type exists because of the last time this happened, and its docstring says why the
    # mistake repeats: a rule attached to one field is a rule the next field does not inherit.
    q: Annotated[
        StorableText,
        Query(max_length=32, description="prefix of the symbol, e.g. 'eur' or 'aap'"),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SymbolSearchOut:
    """Symbols starting with `q`, plus where the list came from and when.

    ⚠️ **The provenance is not decoration.** An empty `symbols` list means one of two very
    different things — "no symbol starts with those letters" or "nobody has ever synced this
    broker" — and a screen that cannot tell them apart shows "no results" to a user whose real
    problem is that the catalogue does not exist yet. `snapshot` is `null` in exactly the
    second case, which is the same NULL-is-not-zero distinction the instruments table already
    makes about spreads.
    """
    taken = snapshot_taken_at(session)
    return SymbolSearchOut.build(
        symbols=search_symbols(session, q, limit=limit),
        server=None if taken is None else taken[0],
        synced_at=None if taken is None else taken[1],
    )


# One request's worth for "choose the whole market" — a sweep's own ceiling on markets.
_MAX_BROWSE = 500


@router.get("/symbols/markets", response_model=MarketsOut)
def markets(session: SessionDep) -> MarketsOut:
    """How many symbols the broker lists in each market — the browser's tabs (02/10)."""
    listed = browse_symbols(session)
    counts = Counter(one.market for one in listed)
    collected = Counter(one.market for one in listed if one.match.catalogued)
    taken = snapshot_taken_at(session)
    return MarketsOut(
        markets=[
            MarketOut(key=key, label=label, count=counts[key], collected=collected[key])
            for key, label in MARKETS
        ],
        snapshot=None if taken is None else SymbolSnapshotOut(server=taken[0], synced_at=taken[1]),
    )


@router.get("/symbols/browse", response_model=SymbolBrowseOut)
def browse(  # noqa: PLR0913, PLR0917 — one query parameter per filter
    session: SessionDep,
    market: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[StorableText, Query(max_length=64)] = "",
    collected: bool = False,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=_MAX_BROWSE)] = DEFAULT_LIMIT,
) -> SymbolBrowseOut:
    """One page of the broker's symbols in `market` (all of them when left out) whose symbol or
    description contains `q`, alphabetical — his ask (02/10): hundreds of symbols, chosen a
    market at a time, never all on one screen. `collected` keeps only those with candles.

    The ceiling on `limit` is a sweep's own: "choose the whole market" asks for every symbol of
    it at once, and a screen still shows them a page at a time."""
    needle = q.strip().lower()
    found = [
        one
        for one in browse_symbols(session)
        if (market is None or one.market == market)
        and (not collected or one.match.catalogued)
        and (
            not needle
            or needle in one.match.symbol.lower()
            or needle in (one.match.name or "").lower()
            or needle in (one.match.description or "").lower()
        )
    ]
    return SymbolBrowseOut(
        total=len(found),
        offset=offset,
        limit=limit,
        items=[
            BrowsedSymbolOut(
                symbol=one.match.name or one.match.symbol,
                ticker=one.match.symbol,
                description=one.match.description,
                path=one.match.path,
                market=one.market,
                catalogued=one.match.catalogued,
                spread_points=one.spread_points,
                broker=one.match.broker,
            )
            for one in found[offset : offset + limit]
        ],
    )


@router.post("/symbols/sync", response_model=EnqueuedOut, status_code=status.HTTP_202_ACCEPTED)
async def sync(session: SessionDep, queue: QueueDep) -> EnqueuedOut:
    """Ask the host agent to photograph the broker's catalogue again. Returns immediately.

    `202`, not `200`, and the difference is honest rather than pedantic: this handler has no
    way to know whether a terminal is even running. If the agent is down the job simply waits,
    and the screen keeps serving the previous snapshot — which is the behaviour worth having,
    because the alternative is a search box that breaks whenever MetaTrader is closed.

    ⚠️ **No `_job_id`, and the first version of this had one.** A fixed id looked like free
    idempotence — "a user leaning on the button coalesces into one job" — and it does, for an
    hour. arq refuses to enqueue an id that still has a *result* in Redis, and results live for
    `keep_result` seconds, 3600 by default. Measured here: after one successful sync the key
    `arq:result:sync_symbols:pending` sat with 2187 s left, and every further press returned
    `202` and did nothing at all. Somebody who had just switched brokers would press it, see
    success, and get the old list.

    So the job is enqueued plainly. Pressing twice runs it twice, which costs 0.17 s and lands
    on the same snapshot — the job replaces the table, so repeating it is a no-op by
    construction. Not letting a person hammer the button is the *screen's* job, and it does it
    by disabling while the request is in flight; a queue trick that quietly stops working after
    the first success is not idempotence, it is a rate limit nobody asked for.
    """
    # ⚠️ **One job per broker** (ADR-0032): each agent photographs its own terminal, so each
    # broker's queue gets one; the legacy queue too, for an agent started without a broker.
    for slug in [None, *broker_slugs(session)]:
        await queue.enqueue_job(SYNC_SYMBOLS, _queue_name=collect_queue(slug))
    return EnqueuedOut(job=SYNC_SYMBOLS)


@router.get("/symbols/{symbol}/history", response_model=SymbolHistoryOut)
def history(session: SessionDep, symbol: Symbol, timeframe: Timeframe) -> SymbolHistoryOut:
    """What the last probe found about this series.

    ⚠️ **404 when nobody has probed yet, which is not the same as a symbol with no bars.** One
    invites a click and the other does not, and a screen that saw an empty row for both would
    tell somebody their broker has no history for EURUSD because nobody had asked yet.
    """
    found = read_history(session, symbol, timeframe)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{symbol} {timeframe} has not been probed yet",
        )
    return SymbolHistoryOut.build(found)


@router.post(
    "/symbols/{symbol}/probe", response_model=EnqueuedOut, status_code=status.HTTP_202_ACCEPTED
)
async def probe(
    session: SessionDep, queue: QueueDep, symbol: Symbol, timeframe: Timeframe
) -> EnqueuedOut:
    """Ask the host agent to measure this series. Returns immediately.

    ⚠️ 202 is not politeness here, it is the measurement: a cold H4 took **207 seconds** on this
    broker, because the terminal downloads the history while answering. A handler that waited
    would hold a request open for three and a half minutes and time out somewhere in between.
    """
    await queue.enqueue_job(
        PROBE_HISTORY, symbol, timeframe, _queue_name=collect_queue_for(session, symbol)
    )
    return EnqueuedOut(job=PROBE_HISTORY)
