"""`/sweeps` — several catalogue entries, over several charts, over several markets.

The product a study and a basket each refuse to take. A study varies the parameters and holds
the market still; a basket varies the market and holds the parameters still. Neither answers
"run every variation I care about over everything I have collected", which is what a shelf full
of entries is for.

⚠️ **Writing the timeframe into each document makes PR-238's equality rule vacuous here, by
construction.** That rule refuses a run whose timeframe disagrees with its document's, because
a higher-timeframe filter is built from the document's own bar width. A sweep that set only the
run's timeframe would trip it on every point; setting both from one value means they cannot
disagree, and the only check left is the DSL's own semantics — which `refusal_of` already gives
in the words a screen knows how to render.

⚠️ **The preview subtracts what will not run; the launch drops nothing quietly.** A combination
the DSL refuses is reported per entry, and a (market, chart) with no candles in the window is
named as uncovered, before anything is written. Launched without collecting, the sweep enqueues
exactly what the preview counted and keeps the uncovered pairs as `Sweep.skipped`; told to
collect, it runs those pairs too, once their downloads land. A sweep that silently dropped points
would draw a map of a space it never searched, and it would look exactly like a map of one it
did — which is why a hole is always written down, never merely left.
"""

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer, selectinload

from tradeforge_api import sweep_dashboard as dashboard
from tradeforge_api.coverage import describe, to_collect, uncovered_markets
from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.grid import GridPoint
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE, RUN_BACKTEST
from tradeforge_api.routers.backtests import list_item
from tradeforge_api.routers.strategies import refusal_of
from tradeforge_api.routers.studies import aggregate_points, strategies_for
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    CreatedSweep,
    CreateSweep,
    DatasetColumnOut,
    DatasetDictionaryOut,
    DatasetOmissionOut,
    GridRefusal,
    PlannedCollection,
    PreviewSweepRequest,
    SweepDashboardOut,
    SweepEntryOut,
    SweepEntryPreview,
    SweepListEntry,
    SweepListItem,
    SweepOut,
    SweepPreview,
    SweepRunCounts,
    SweepRunOut,
    SweepsPage,
    UncoveredMarket,
)
from tradeforge_api.sweep import (
    SweepDocument,
    SweepError,
    documents_for,
    points_in,
    size_refusal,
)
from tradeforge_api.sweep_dataset import CAVEATS, OMITTED, ROW, DatasetRun, columns_for, to_csv
from tradeforge_collector.collect import year_slices
from tradeforge_db.collections import create_collection
from tradeforge_db.models import (
    Backtest,
    BacktestCollection,
    BacktestMetrics,
    BacktestStatus,
    CatalogEntry,
    Collection,
    Instrument,
    Strategy,
    Sweep,
)

router = APIRouter(tags=["sweeps"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}

# The type's own limit, as the other paged routers bound it: every value it admits is valid SQL
# that returns an empty page, so nothing legitimate is refused.
_MAX_OFFSET = 9_223_372_036_854_775_807  # 2**63 - 1, Postgres bigint


def _entries(session: SessionDep, ids: list[uuid.UUID]) -> list[tuple[CatalogEntry, Strategy]]:
    """The chosen entries with their strategies, **in the order they were asked for**.

    ⚠️ The order is restored rather than left to the database. `IN (...)` returns rows in
    whatever order suits the plan, and a preview whose entries come back shuffled is a preview
    whose refusals sit beside the wrong label.
    """
    rows = session.execute(
        select(CatalogEntry, Strategy)
        .join(Strategy, Strategy.id == CatalogEntry.strategy_id)
        .where(CatalogEntry.id.in_(ids))
    ).all()
    found = {entry.id: (entry, strategy) for entry, strategy in rows}
    missing = [str(one) for one in ids if one not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown catalogue entries: {', '.join(missing)}",
        )
    return [found[one] for one in ids]


def _expand(
    pairs: list[tuple[CatalogEntry, Strategy]], timeframes: list[str]
) -> list[SweepDocument]:
    """Every document the sweep would write, or a 422 naming the entry that cannot expand."""
    out: list[SweepDocument] = []
    for entry, strategy in pairs:
        try:
            out.extend(
                documents_for(
                    entry_id=str(entry.id),
                    entry_name=entry.name,
                    definition=strategy.definition,
                    grid=entry.grid,
                    timeframes=timeframes,
                )
            )
        except SweepError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
            ) from exc
    return out


def _combinations(
    documents: list[SweepDocument], symbols: list[str], skipped: list[UncoveredMarket]
) -> list[tuple[SweepDocument, str]]:
    """Every (document, market) the sweep runs: the product, less the pairs with nothing to read.

    ⚠️ **Skipped by pair, not by market.** A market can hold M15 and not H4, and dropping it from
    every chart for the want of one would leave out runs that could have read every bar.
    """
    empty = {(market.symbol, market.timeframe) for market in skipped}
    return [
        (doc, symbol)
        for doc in documents
        for symbol in symbols
        if (symbol, doc.timeframe) not in empty
    ]


def _worth_naming(
    uncovered: list[UncoveredMarket], documents: list[SweepDocument]
) -> list[UncoveredMarket]:
    """The uncovered pairs on a chart where some point can run — the ones that are a hole.

    ⚠️ **A chart the DSL refused everywhere is not missing data.** An H4 filter cannot run on H4,
    so EURUSD H4 without candles has no run to lose: naming it as skipped would offer a download
    that produces nothing, and would blur the two absences `Sweep.skipped` exists to keep apart.
    That chart is reported where it belongs, as the preview's refusals.
    """
    charts = {doc.timeframe for doc in documents}
    return [market for market in uncovered if market.timeframe in charts]


def _nothing_to_read(
    documents: list[SweepDocument],
    combinations: list[tuple[SweepDocument, str]],
    skipped: list[UncoveredMarket],
) -> str | None:
    """Why a sweep whose every runnable point landed on a skipped pair cannot be launched.

    ⚠️ **Asked before the size rule, and only when there were points to run.** With every pair
    skipped the sweep is empty, and the size rule's "no combination in this sweep can run" would
    blame the grid for what is missing data. With no runnable point at all, the grid *is* to
    blame, and the size rule says so.
    """
    if documents and not combinations:
        return "no candles in this window for: " + ", ".join(map(describe, skipped))
    return None


@router.post("/sweeps/preview", response_model=SweepPreview, responses={**_NOT_FOUND, **_BAD_BODY})
def preview_sweep(request: PreviewSweepRequest, session: SessionDep) -> SweepPreview:
    """What this sweep would enqueue, without enqueuing any of it.

    ⚠️ **The browser cannot answer this and must not try.** Whether a combination can run is the
    DSL's semantics, and those live in Python once — a screen that reimplemented them would be a
    second copy of the contract, wrong the day either changed.

    Reports rather than decides, like the study's preview and for the same reason: a person
    fixing one axis per round trip is the round trip this endpoint exists to remove. What would
    make the launch refuse — nothing runnable, every pair without data — is reported as an
    `error` instead, in the launch's own words. Never its size: there is no cap (18/09).
    """
    pairs = _entries(session, request.entry_ids)

    # ⚠️ **The same guard the launch has, and the preview needs it more.** Without it a
    # backwards window is not a window any dataset can overlap, so `uncovered_markets` can name
    # a market that is collected and the screen reads "no candles in this window; move the
    # window or collect them first" — blaming the data for a typo, and sending a person to run
    # a backfill they do not need. Refused the
    # same way and in the same words the launch refuses it, so one request cannot get two
    # verdicts from the two endpoints.
    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    timeframes = list(request.timeframes)
    documents = _expand(pairs, timeframes)

    per_entry: list[SweepEntryPreview] = []
    for entry, _strategy in pairs:
        mine = [doc for doc in documents if doc.entry_id == str(entry.id)]
        refusals = [
            GridRefusal(label=doc.label, values=dict(doc.values), reason=reason)
            for doc in mine
            if (reason := refusal_of(dict(doc.document))) is not None
        ]
        per_entry.append(
            SweepEntryPreview(
                entry_id=entry.id,
                name=entry.name,
                points=points_in(entry.grid),
                refusals=refusals,
            )
        )

    runnable_documents = [doc for doc in documents if refusal_of(dict(doc.document)) is None]
    uncovered = _worth_naming(
        uncovered_markets(
            session, list(request.symbols), timeframes, request.date_from, request.date_to
        ),
        runnable_documents,
    )

    # ⚠️ Counted as the launch counts them when told not to collect: the pairs with no candles
    # are skipped, not refused (his rule, 18/09), so they subtract rather than block.
    # ⚠️ The empty sweep and the all-skipped one are asked of the same functions the launch asks, so
    # the two endpoints cannot answer this in different words. Reported as an `error` rather than
    # raised, which is the one thing that *is* different: a preview that raised would have
    # nothing to preview.
    combinations = _combinations(runnable_documents, list(request.symbols), uncovered)
    runs = len(combinations)
    error = _nothing_to_read(runnable_documents, combinations, uncovered) or size_refusal(runs)

    return SweepPreview(
        runs=runs,
        documents=len(documents),
        entries=per_entry,
        uncovered=uncovered,
        error=error,
    )


@router.post(
    "/sweeps",
    response_model=CreatedSweep,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
async def create_sweep(request: CreateSweep, session: SessionDep, queue: QueueDep) -> CreatedSweep:
    """Write the sweep, its strategies and its runs in one transaction, then enqueue.

    **All or nothing in the face of a refusal.** Every refusal is decided before anything is
    written: a sweep that half-exists is worse than one that was refused, because the caller
    asked one question about a space, and four hundred runs plus an error answers a question
    nobody asked. The same doctrine as the study, one axis up.

    ⚠️ **Two kinds of point are left out, and both are named.** A combination the DSL refuses is
    not part of the space — enqueuing it to fail would spend a worker to re-learn what
    `assert_executable` knew — and the preview names it. A pair with no candles in the window is
    part of the space and has nothing to read (his answer "do not collect", 18/09): it is left out
    and written on the sweep as `skipped`. What must never happen is a point that *could* have
    run disappearing without a name — and since 18/09 no point is left out for the size of the
    sweep either: there is no cap.
    """
    pairs = _entries(session, request.entry_ids)
    timeframes = list(request.timeframes)

    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    found = {
        instrument.symbol: instrument
        for instrument in session.scalars(
            select(Instrument).where(Instrument.symbol.in_(request.symbols))
        )
    }
    unknown = [symbol for symbol in request.symbols if symbol not in found]
    if unknown:
        # Every bad symbol at once. A caller fixing a typo list one round trip at a time is a
        # caller the API is failing — the same answer `/baskets` gives.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown symbols: {', '.join(unknown)}",
        )

    planned = (
        to_collect(
            session,
            symbols=list(request.symbols),
            timeframes=timeframes,
            date_from=request.date_from,
            date_to=request.date_to,
        )
        if request.collect_missing
        else []
    )
    collectable = {(market.symbol, market.timeframe): market for market in planned}

    # ⚠️ **Skipped and named, never enqueued** — his answer "do not collect" (18/09). A run that
    # cannot read a single candle spends a worker to re-learn what the `datasets` index already
    # knows, which is what the first real sweep did nine times out of twelve. The sweep used to
    # refuse the whole launch over one such pair; the rest runs now, and the hole is written on
    # the sweep itself (`Sweep.skipped`) so the map never passes for complete.
    # ⚠️ A pair with something to fetch is not skipped when told to collect; an empty plan is not
    # "covered", though. A window wholly in the future, or older than the broker's first bar, has
    # nothing to download and nothing to read, and is skipped like any other.
    documents = [
        doc for doc in _expand(pairs, timeframes) if refusal_of(dict(doc.document)) is None
    ]
    skipped = [
        market
        for market in _worth_naming(
            uncovered_markets(
                session, list(request.symbols), timeframes, request.date_from, request.date_to
            ),
            documents,
        )
        if (market.symbol, market.timeframe) not in collectable
    ]
    combinations = _combinations(documents, list(request.symbols), skipped)
    # The same questions the preview asked, of the same functions, so the words a person read on
    # the screen are the words they get back from the button.
    refusal = _nothing_to_read(documents, combinations, skipped) or size_refusal(len(combinations))
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refusal)
    # ⚠️ Only after the refusals, which need every runnable document to tell "all skipped" from
    # "nothing runnable". A chart skipped on every market leaves documents no run reads; writing
    # them would put points on the map with nothing measured under them.
    # ⚠️ Read from `combinations`, never re-derived: the rule for which pairs run has one owner,
    # and a second copy would let the preview's count and the runs written drift apart. Keyed by
    # `id()`, which holds because every document stays alive in `documents` until the runs exist.
    markets_of: dict[int, list[str]] = {}
    for doc, symbol in combinations:
        markets_of.setdefault(id(doc), []).append(symbol)
    documents = [doc for doc in documents if id(doc) in markets_of]

    sweep = Sweep(
        entry_ids=[str(one) for one in request.entry_ids],
        symbols=list(request.symbols),
        timeframes=timeframes,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
        skipped=[market.model_dump() for market in skipped],
    )
    session.add(sweep)

    # ⚠️ **Reused, never reimplemented.** `strategies_for` is where the reuse-or-version decision
    # lives — a point's document is written once and a later sweep finds it rather than colliding
    # on `(name, version)`. Copying that logic here would be a second answer to a question the
    # study already answers subtly and correctly.
    points = [GridPoint(values=dict(doc.values), document=dict(doc.document)) for doc in documents]
    strategies = strategies_for(session, points)
    session.add_all(strategies)
    session.flush()

    # ⚠️ **The coordinates are written down now, keyed by the strategy they produced.** The
    # alternative is recovering them later from the document's name — and a name is a caption:
    # `{entry} [{label}]` splits cleanly until one entry's name is a prefix of another's, or a
    # value contains the separator. Both are things a person will do, and neither would raise.
    sweep.points = [
        {
            "strategy_id": str(strategy.id),
            "entry_id": doc.entry_id,
            "label": doc.label,
            "values": dict(doc.values),
        }
        for doc, strategy in zip(documents, strategies, strict=True)
    ]

    runs: list[Backtest] = []
    paired: list[tuple[SweepDocument, str, Backtest]] = []
    for doc, strategy in zip(documents, strategies, strict=True):
        for symbol in markets_of[id(doc)]:
            run = Backtest(
                sweep=sweep,
                strategy_id=strategy.id,
                instrument_id=found[symbol].id,
                # The document's timeframe and the run's are one value, read from one place —
                # which is what makes PR-238's equality rule unreachable here.
                timeframe=doc.timeframe,
                date_from=request.date_from,
                date_to=request.date_to,
                initial_capital=request.initial_capital,
                cost_model=request.cost_model,
                status=BacktestStatus.QUEUED,
                engine_version=ENGINE_VERSION,
            )
            runs.append(run)
            paired.append((doc, symbol, run))
    session.add_all(runs)

    collections = _collections_for(session, paired, collectable, found)

    try:
        session.commit()
    except IntegrityError as exc:
        # Two identical sweeps launched at once: both read before either wrote, and the loser
        # meets the unique index on `(name, version)`. The rollback is total — see the study's
        # own note, which this is the same collision as, one axis up.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another sweep created these strategies at the same time; try again",
        ) from exc

    # ⚠️ After the commit, and with the run's own id as the job id. A worker is fast enough to
    # claim a job before an uncommitted row is visible; and the derived job id makes the enqueue
    # idempotent, so a crash halfway through this loop is recovered by re-sending rather than by
    # working out which of two thousand runs were reached.
    # ⚠️ **That claim is the runs' alone.** The downloads carry no job id, as the basket's do not:
    # re-sending them queues each window again, and a crash before one is sent leaves it
    # `queued` with its runs deferring until the queue has been silent for `WAIT_LIMIT`.
    for collection in collections:
        await queue.enqueue_job(COLLECT_RANGE, str(collection.id), _queue_name=COLLECT_QUEUE)
    for run in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(run.id), _job_id=str(run.id))

    return CreatedSweep(id=sweep.id, runs=len(runs), skipped=skipped)


def _collections_for(
    session: Session,
    paired: list[tuple[SweepDocument, str, Backtest]],
    collectable: dict[tuple[str, str], PlannedCollection],
    found: dict[str, Instrument],
) -> list[Collection]:
    """Write one collection per window of each pair to collect, linked to every run over it.

    ⚠️ **Grouped by pair before anything is written — this is what the basket's loop cannot be
    copied for.** A basket has one run per market, so a collection per run is a collection per
    market. A sweep has every point of every entry over the same pair, and a collection per run
    would download the same window once per point. The link table's key is the pair of ids, so
    many runs waiting on one collection is a row each, and the worker needs no change: each run
    asks whether *its* collections have landed.
    """
    waiting: dict[tuple[str, str], list[Backtest]] = {}
    for doc, symbol, run in paired:
        if (symbol, doc.timeframe) in collectable:
            waiting.setdefault((symbol, doc.timeframe), []).append(run)
    if not waiting:
        return []

    session.flush()  # the runs' ids, for the links
    collections: list[Collection] = []
    for (symbol, timeframe), runs in waiting.items():
        for window in collectable[symbol, timeframe].windows:
            collection = create_collection(
                session,
                symbol=symbol,
                timeframe=timeframe,
                date_from=window.date_from,
                date_to=window.date_to,
                # The class the catalogue already decided, as the basket does it: the broker's
                # tree path must not refuse a collection the launch itself asked for.
                asset_class=found[symbol].asset_class,
                years_total=len(year_slices(window.date_from, window.date_to)),
            )
            collections.append(collection)
            session.add_all(
                BacktestCollection(backtest_id=run.id, collection_id=collection.id) for run in runs
            )
    return collections


@router.get("/sweeps", response_model=SweepsPage)
def list_sweeps(
    session: SessionDep,
    *,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
) -> SweepsPage:
    """Every sweep, newest first: what it asked, and how many of its runs have landed.

    ⚠️ **This is the only way back to a sweep once its tab is closed.** The run log hides the runs
    a grid generated, and `/sweeps/{id}` needs an id nobody wrote down — before this list, the
    sweeps of three evenings were reachable only through the browser's history.

    ⚠️ **The same queries for a page of one sweep as for a page of many**: the total, the sweeps,
    one grouped count
    of their runs, and the shelf names of their entries. Reading each sweep back in full would
    work, and would ship every run of every sweep on the page to say "480 of 500 done". `points`
    is deferred for the same reason: it is one element per grid point, and no field of this
    response reaches it. Held by `test_listing_sweeps_costs_the_same_however_many_there_are`.
    """
    total = session.scalar(select(func.count()).select_from(Sweep)) or 0
    sweeps = session.scalars(
        select(Sweep)
        .options(defer(Sweep.points))
        # `created_at` is the launch transaction's start, so it only ties between sweeps launched
        # in the same instant; the id breaks that tie so a page boundary cannot move between reads.
        .order_by(Sweep.created_at.desc(), Sweep.id)
        .limit(limit)
        .offset(offset)
    ).all()

    counts: dict[uuid.UUID, dict[BacktestStatus, int]] = {
        sweep.id: dict.fromkeys(BacktestStatus, 0) for sweep in sweeps
    }
    for sweep_id, run_status, how_many in session.execute(
        select(Backtest.sweep_id, Backtest.status, func.count())
        .where(Backtest.sweep_id.in_(list(counts)))
        .group_by(Backtest.sweep_id, Backtest.status)
    ).all():
        counts[sweep_id][run_status] = how_many

    asked = {uuid.UUID(one) for sweep in sweeps for one in sweep.entry_ids}
    names = {
        entry.id: entry.name
        for entry in session.scalars(select(CatalogEntry).where(CatalogEntry.id.in_(asked)))
    }

    items: list[SweepListItem] = []
    for sweep in sweeps:
        tally = counts[sweep.id]
        items.append(
            SweepListItem(
                id=sweep.id,
                created_at=sweep.created_at,
                entries=[
                    SweepListEntry(entry_id=one, name=names.get(one))
                    for one in (uuid.UUID(raw) for raw in sweep.entry_ids)
                ],
                symbols=list(sweep.symbols),
                timeframes=list(sweep.timeframes),
                date_from=sweep.date_from,
                date_to=sweep.date_to,
                runs=SweepRunCounts(
                    total=sum(tally.values()),
                    done=tally[BacktestStatus.DONE],
                    running=tally[BacktestStatus.RUNNING],
                    queued=tally[BacktestStatus.QUEUED],
                    failed=tally[BacktestStatus.FAILED],
                ),
            )
        )

    return SweepsPage(total=total, limit=limit, offset=offset, items=items)


@router.get("/sweeps/dashboard", response_model=SweepDashboardOut)
def get_sweep_dashboard(
    session: SessionDep,
    *,
    launched_from: Annotated[
        AwareDatetime | None, Query(description="first launch instant included")
    ] = None,
    launched_to: Annotated[
        AwareDatetime | None, Query(description="first launch instant excluded")
    ] = None,
) -> SweepDashboardOut:
    """Every sweep launched in `[launched_from, launched_to)`, summarised at once.

    ⚠️ **Declared before `/sweeps/{sweep_id}`, and the order is the route.** FastAPI tries paths
    in declaration order, and `dashboard` would otherwise be read as a malformed sweep id — a 422
    for a page that exists.

    ⚠️ **Instants with an offset, never bare dates.** "Launched on the 15th" depends on whose
    clock: a sweep launched at 22:00 in São Paulo is the 16th in UTC. The screen turns the days a
    person picked into instants in their own zone, and this endpoint only compares; a date
    without an offset is refused rather than guessed at.

    Half-open, so two adjacent windows never count the same sweep twice. Either end may be
    left out.

    ⚠️ **`points` is read here, unlike the history list.** It is the only record of which entry a
    run belongs to — the strategy's name is a caption, and recovering the entry from it breaks
    the day one entry's name is a prefix of another's. The equity curve stays deferred.
    """
    if launched_from is not None and launched_to is not None and launched_to <= launched_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="launched_to must come after launched_from",
        )

    window = []
    if launched_from is not None:
        window.append(Sweep.created_at >= launched_from)
    if launched_to is not None:
        window.append(Sweep.created_at < launched_to)
    sweeps = session.scalars(
        select(Sweep).where(*window).order_by(Sweep.created_at, Sweep.id)
    ).all()

    asked = {uuid.UUID(one) for sweep in sweeps for one in sweep.entry_ids}
    names = {
        str(entry.id): entry.name
        for entry in session.scalars(select(CatalogEntry).where(CatalogEntry.id.in_(asked)))
    }
    # Per sweep: each run is looked up in the points of the sweep that launched it. A document
    # is reused by every later sweep of the same entry, so the sweep is the only key that says,
    # without a second lookup, which record wrote this run's coordinates.
    entry_of = {
        sweep.id: {str(point["strategy_id"]): str(point["entry_id"]) for point in sweep.points}
        for sweep in sweeps
    }

    rows = session.execute(
        select(Backtest, Instrument.symbol)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id.in_(list(entry_of)))
        # Launch order, so which copy of a repeated measurement is kept does not change between
        # two reads of the same data.
        .order_by(Backtest.created_at, Backtest.id)
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
    ).all()

    runs: list[dashboard.DashboardRun] = []
    for run, symbol in rows:
        if run.sweep_id is None:  # pragma: no cover — the filter above selects by sweep
            continue
        entry_id = entry_of[run.sweep_id].get(str(run.strategy_id), "")
        metrics = run.metrics
        runs.append(
            dashboard.DashboardRun(
                sweep_id=str(run.sweep_id),
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                symbol=symbol,
                timeframe=run.timeframe,
                status=run.status,
                initial_capital=run.initial_capital,
                measurement=(
                    run.strategy_id,
                    run.instrument_id,
                    run.timeframe,
                    run.date_from,
                    run.date_to,
                    run.initial_capital,
                    json.dumps(run.cost_model, sort_keys=True),
                    run.engine_version,
                ),
                result=None
                if metrics is None
                else dashboard.RunResult(
                    net_profit=metrics.net_profit,
                    total_trades=metrics.total_trades,
                    win_rate=metrics.win_rate,
                    profit_factor=metrics.profit_factor,
                    expectancy=metrics.expectancy,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                ),
            )
        )

    listed = [
        dashboard.DashboardSweepRow(
            sweep_id=str(sweep.id),
            created_at=sweep.created_at,
            entry_names=[names.get(one) for one in sweep.entry_ids],
            skipped=tuple((one["symbol"], one["timeframe"]) for one in sweep.skipped),
        )
        for sweep in sweeps
    ]
    # Each measurement once for everything that summarises results; every run for what counts
    # launches. See the module's note on why a copy must not vote twice.
    measured = dashboard.distinct(runs)
    win_rate, profit_factor, expectancy = dashboard.ratios(measured)
    return SweepDashboardOut(
        launched_from=launched_from,
        launched_to=launched_to,
        totals=dashboard.totals(listed, runs),
        overall=dashboard.summarise("all", "All sweeps", measured),
        win_rate=win_rate,
        profit_factor=profit_factor,
        expectancy=expectancy,
        by_entry=dashboard.by_entry(measured),
        by_symbol=dashboard.by_symbol(measured),
        by_timeframe=dashboard.by_timeframe(measured),
        sweeps=dashboard.per_sweep(listed, runs),
    )


@router.get("/sweeps/{sweep_id}", response_model=SweepOut, responses=_NOT_FOUND)
def get_sweep(sweep_id: uuid.UUID, session: SessionDep) -> SweepOut:
    """A sweep read back: the question that was asked, and every run it became.

    ⚠️ **The coordinates come from `sweeps.points`, never from the strategy's name.** The name
    carries the label (`9.1 sem filtro [M15 · period=5]`) because that is what a run log row
    shows — but it is a caption, and recovering axis values by splitting one works until an
    entry's name is a prefix of another's or a value contains the separator. The first draft of
    this endpoint did exactly that; the column exists because of it.
    """
    sweep, entries, coordinates, rows = _read_sweep(session, sweep_id)

    out: list[SweepRunOut] = []
    scored: dict[str, list[tuple[Backtest, str]]] = {one: [] for one in sweep.entry_ids}
    for run, strategy, instrument in rows:
        point = coordinates.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        entry = entries.get(entry_id)
        label = str(point.get("label", ""))
        out.append(
            SweepRunOut(
                entry_id=uuid.UUID(entry_id) if entry_id else uuid.UUID(int=0),
                # ⚠️ The shelf label if the entry is still there, and the document's own name if
                # it is not. Removing an entry is allowed by design and must not blank a finished
                # sweep — what it costs is the label, never the measurement.
                entry_name=entry.name if entry is not None else strategy.name,
                label=label,
                values=dict(point.get("values", {})),
                run=list_item(run, instrument.symbol, strategy.name, strategy.version),
            )
        )
        if entry_id in scored:
            # The symbol leads because the point label repeats once per market: `M15 · period=9`
            # over three symbols is three runs, and a best that named only the point would name
            # all three of them.
            scored[entry_id].append((run, f"{instrument.symbol} · {label}"))

    summaries = [
        SweepEntryOut(
            entry_id=uuid.UUID(entry_id),
            entry_name=shelved.name if (shelved := entries.get(entry_id)) is not None else None,
            aggregate=aggregate_points(scored[entry_id], sweep.initial_capital),
        )
        for entry_id in sweep.entry_ids
    ]

    return SweepOut(
        id=sweep.id,
        entry_ids=[uuid.UUID(one) for one in sweep.entry_ids],
        symbols=list(sweep.symbols),
        timeframes=list(sweep.timeframes),
        date_from=sweep.date_from,
        date_to=sweep.date_to,
        initial_capital=sweep.initial_capital,
        created_at=sweep.created_at,
        entries=summaries,
        runs=out,
        skipped=[UncoveredMarket.model_validate(one) for one in sweep.skipped],
    )


_SweepRows = list[tuple[Backtest, Strategy, Instrument]]


def _read_sweep(
    session: SessionDep, sweep_id: uuid.UUID
) -> tuple[Sweep, dict[str, CatalogEntry], dict[str, dict[str, Any]], _SweepRows]:
    """A sweep, its surviving shelf entries, its coordinates by strategy, and every run joined.

    ⚠️ **One read for every view of a sweep**, the screen's and the dataset's. The query below
    carries the guard that keeps a poll from dragging every equity curve out of Postgres, and a
    second query written for the export would be a second place for that defect to come back.
    """
    sweep = session.get(Sweep, sweep_id)
    if sweep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")

    entries = {
        str(entry.id): entry
        for entry in session.scalars(
            select(CatalogEntry).where(CatalogEntry.id.in_([uuid.UUID(x) for x in sweep.entry_ids]))
        )
    }
    # Keyed by the strategy each point produced, which is the join the runs already carry.
    coordinates = {str(point["strategy_id"]): point for point in sweep.points}

    rows = session.execute(
        select(Backtest, Strategy, Instrument)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id == sweep.id)
        .order_by(Backtest.created_at, Strategy.name, Instrument.symbol)
        # ⚠️ **The curve is deferred, and on this endpoint that is not an optimisation.** The
        # screen polls it every few seconds while the runs land, and each response carries every
        # run: loaded lazily, row by row, with the curve (the largest measured is 856 kB), one
        # poll of a two-thousand-run sweep would read the whole history of every finished run
        # to serve fields that never reach the response. The study's read defers it too, as a
        # column no field of its response reaches; here the poll is what turns that into a bill.
        # Held by `test_reading_a_sweep_costs_the_same_however_many_runs_it_holds`.
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
    ).all()

    return (
        sweep,
        entries,
        coordinates,
        [(run, strategy, instrument) for run, strategy, instrument in rows],
    )


def _dataset_runs(session: SessionDep, sweep_id: uuid.UUID) -> list[DatasetRun]:
    """The sweep's runs as dataset rows, in the order `_read_sweep` reads them.

    That is launch time — which ties across a whole sweep, written in one transaction — then
    strategy name (`{entry} [{label}]`), then symbol. Not the screen's order, which groups the runs
    by entry as the request listed them.

    `entry_name` is null for an entry removed from the shelf — not the generated strategy's name
    that `SweepRunOut.entry_name` falls back to, which carries one point's label and would name
    the entry after that point.
    """
    sweep, entries, coordinates, rows = _read_sweep(session, sweep_id)
    out: list[DatasetRun] = []
    for run, strategy, instrument in rows:
        point = coordinates.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        shelved = entries.get(entry_id)
        out.append(
            DatasetRun(
                sweep_id=str(sweep.id),
                run=run,
                entry_id=entry_id,
                entry_name=None if shelved is None else shelved.name,
                strategy_version=strategy.version,
                symbol=instrument.symbol,
                asset_class=str(instrument.asset_class),
                values=dict(point.get("values", {})),
            )
        )
    return out


@router.get(
    "/sweeps/{sweep_id}/dataset.csv",
    response_class=Response,
    responses={
        **_NOT_FOUND,
        status.HTTP_200_OK: {
            "content": {"text/csv": {}},
            "description": "One row per run. `/sweeps/{sweep_id}/dataset/dictionary` says what "
            "every column is.",
        },
    },
)
def get_sweep_dataset(sweep_id: uuid.UUID, session: SessionDep) -> Response:
    """The sweep as a table for a model: one row per run, every column described by the dictionary.

    ⚠️ **Runs that have not finished, and runs that failed, keep their rows.** A download taken
    while the sweep is still draining is a true picture of that moment, and a failed run is part
    of the space that was searched — a file that dropped either would describe a sweep nobody ran.
    """
    return Response(
        content=to_csv(_dataset_runs(session, sweep_id)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="sweep-{sweep_id}.csv"'},
    )


@router.get(
    "/sweeps/{sweep_id}/dataset/dictionary",
    response_model=DatasetDictionaryOut,
    responses=_NOT_FOUND,
)
def get_sweep_dataset_dictionary(sweep_id: uuid.UUID, session: SessionDep) -> DatasetDictionaryOut:
    """What every column of this sweep's dataset is, what was left out, and what to beware of.

    Read from the same rows as the file, because the grid columns are the sweep's own: asking a
    sweep with no `period` axis for its dictionary must not describe a `param:…period` column.
    """
    runs = _dataset_runs(session, sweep_id)
    return DatasetDictionaryOut(
        row=ROW,
        caveats=list(CAVEATS),
        columns=[
            DatasetColumnOut(
                name=column.name,
                role=column.role,
                unit=column.unit,
                description=column.description,
            )
            for column in columns_for(runs)
        ],
        omitted=[DatasetOmissionOut(name=name, reason=reason) for name, reason in OMITTED],
    )
