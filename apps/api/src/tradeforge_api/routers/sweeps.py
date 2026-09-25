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
import secrets
import uuid
from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy import ColumnElement, and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session, defer, selectinload

from tradeforge_api import sweep_dashboard as dashboard
from tradeforge_api.coverage import describe, to_collect, uncovered_markets
from tradeforge_api.deps import QueueDep, SessionDep, SettingsDep
from tradeforge_api.estimates import backtests_time
from tradeforge_api.grid import GridPoint
from tradeforge_api.holdout import (
    Bounds,
    Candidate,
    choose,
    median_of,
    overlaps,
    positive_share,
)
from tradeforge_api.montecarlo import Spread, observed, simulate
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE, RUN_BACKTEST
from tradeforge_api.retention import MIN_TRADES
from tradeforge_api.routers.backtests import failed_collections, list_item
from tradeforge_api.routers.strategies import refusal_of
from tradeforge_api.routers.studies import aggregate_points, strategies_for
from tradeforge_api.runner import ENGINE_VERSION, build_cost_model, swap_rates
from tradeforge_api.schemas import (
    CreatedSweep,
    CreateHoldout,
    CreateMonteCarlo,
    CreateSlicing,
    CreateSweep,
    DatasetColumnOut,
    DatasetDictionaryOut,
    DatasetOmissionOut,
    GridRefusal,
    HoldoutGroup,
    HoldoutOut,
    HoldoutRow,
    HoldoutSide,
    MonteCarloOut,
    MonteCarloPoint,
    PlannedCollection,
    PreviewSweepRequest,
    SimulatedOut,
    SlicedGroup,
    SlicedPoint,
    SliceOut,
    SlicingOut,
    SpreadOut,
    SweepDashboardOut,
    SweepEntryOut,
    SweepEntryPreview,
    SweepListEntry,
    SweepListItem,
    SweepOut,
    SweepPoint,
    SweepPreview,
    SweepRunCounts,
    SweepRunOut,
    SweepRunsPage,
    SweepsPage,
    UncoveredMarket,
)
from tradeforge_api.slices import ClosedTrade, by_blocks, by_year, verdict
from tradeforge_api.sweep import (
    SweepDocument,
    SweepError,
    documents_for,
    points_in,
    shared,
    size_refusal,
)
from tradeforge_api.sweep_dataset import CAVEATS, OMITTED, ROW, DatasetRun, columns_for, to_csv
from tradeforge_api.targets import rungs_across
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
    Recorded,
    SliceMode,
    Strategy,
    Sweep,
    SweepMonteCarlo,
    SweepSlicing,
    Trade,
)
from tradeforge_engine.setup_factory import unread_params

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
def preview_sweep(
    request: PreviewSweepRequest, session: SessionDep, settings: SettingsDep
) -> SweepPreview:
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

    runnable = [doc for doc in documents if refusal_of(dict(doc.document)) is None]
    # ⚠️ Counted as the launch counts them: a point another point answers runs nothing (24/09).
    answered = shared(runnable, unread_params)
    runnable_documents = [doc for doc in runnable if id(doc) not in answered]
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
        shared=len(answered),
        entries=per_entry,
        uncovered=uncovered,
        # Every run of a sweep reads the same window, so what varies from run to run is the
        # chart: the bars in the window depend on the document's timeframe, not on the market.
        backtest_time=backtests_time(
            session,
            ((request.date_from, request.date_to, doc.timeframe) for doc, _symbol in combinations),
            workers=settings.tradeforge_workers,
        ),
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
    costs = _costs_for(request.cost_model, found)

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
    runnable = [doc for doc in _expand(pairs, timeframes) if refusal_of(dict(doc.document)) is None]
    # ⚠️ **One run for points that run the same** (his answer, 24/09): a point that differs from
    # an earlier one only in a parameter its entry point never reads is answered by that one's
    # run. It is still a point of the sweep — written in `points` below, pointing at the run that
    # answers it — and never a run of its own. The same call the preview makes, so the two agree.
    answered = shared(runnable, unread_params)
    documents = [doc for doc in runnable if id(doc) not in answered]
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
    # A point is kept only if the point answering it runs: the same chart, so the same pairs.
    followers = [
        doc for doc in runnable if id(doc) in answered and id(answered[id(doc)]) in markets_of
    ]

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
    # ⚠️ **A point answered by another carries that point's strategy and says so** (`same_as`).
    # The strategy id is the join every read already makes from a run to its coordinates, so a
    # read that knows nothing of this still finds the run's own point — the one without
    # `same_as` — and a read that does (`_points_of`) finds the others beside it.
    strategy_of = {id(doc): strategy for doc, strategy in zip(documents, strategies, strict=True)}
    sweep.points = [
        *(
            {
                "strategy_id": str(strategy_of[id(doc)].id),
                "entry_id": doc.entry_id,
                "label": doc.label,
                "values": dict(doc.values),
            }
            for doc in documents
        ),
        *(
            {
                "strategy_id": str(strategy_of[id(answered[id(doc)])].id),
                "entry_id": doc.entry_id,
                "label": doc.label,
                "values": dict(doc.values),
                "same_as": answered[id(doc)].label,
            }
            for doc in followers
        ),
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
                cost_model=costs[symbol],
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

    return CreatedSweep(id=sweep.id, runs=len(runs), shared=len(followers), skipped=skipped)


def _costs_for(
    cost_model: dict[str, Any], instruments: dict[str, Instrument]
) -> dict[str, dict[str, Any]]:
    """What each market's runs are charged — resolved here, written concretely on every run.

    Two ways to charge each market **its own** costs (24/09): one figure across markets would
    charge EURUSD's ticks to a symbol counted in ticks a thousand times larger.

    * `{"type": "per_market", "markets": {"GBPUSD": {"spread_points": "5", "commission_per_unit":
      "0"}}}` — typed at launch, because they are the broker's and change with it (his ask). Every
      market of the sweep must be named.
    * `{"type": "instrument", "commission_per_unit": "7"}` — each market's measured spread
      (`instruments.default_spread_points`) and one commission per lot.

    Any other model is the same for every market.

    ⚠️ **Refused before anything is written, and the reason named:** a market with no costs to
    charge — a sweep mixing costed and costless runs would rank the costless ones first — and a
    model the engine cannot build, which used to fail inside every one of the sweep's runs.
    """
    kind = cost_model.get("type")
    if kind == "per_market":
        given = cost_model.get("markets")
        markets: dict[str, Any] = given if isinstance(given, dict) else {}
        missing = sorted(symbol for symbol in instruments if symbol not in markets)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="no costs given for: " + ", ".join(missing),
            )
        resolved = {symbol: _typed(markets[symbol]) for symbol in instruments}
    elif kind == "instrument":
        unmeasured = sorted(
            symbol for symbol, one in instruments.items() if one.default_spread_points is None
        )
        if unmeasured:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="no measured spread for: "
                + ", ".join(unmeasured)
                + " — sync the symbols from the broker, or type each market's costs",
            )
        commission = cost_model.get("commission_per_unit", "0")
        resolved = {
            symbol: _spread_and_commission(one.default_spread_points, commission)
            for symbol, one in instruments.items()
        }
    else:
        resolved = {symbol: dict(cost_model) for symbol in instruments}
    for model in resolved.values():
        try:
            build_cost_model(model)
            swap_rates(model)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"cost model cannot be charged: {exc}",
            ) from exc
    return resolved


def _typed(market: object) -> dict[str, Any]:
    """One market's typed costs as the document its runs are charged by: the spread and the
    commission, and the swap per side when either was given — signed, as the broker quotes it."""
    given: dict[str, Any] = market if isinstance(market, dict) else {}
    model = _spread_and_commission(
        given.get("spread_points"), given.get("commission_per_unit", "0")
    )
    long_rate, short_rate = given.get("swap_long_per_lot"), given.get("swap_short_per_lot")
    if long_rate is not None or short_rate is not None:
        model["swap"] = {
            "long_per_lot": str(long_rate if long_rate is not None else "0"),
            "short_per_lot": str(short_rate if short_rate is not None else "0"),
        }
    return model


def _spread_and_commission(spread: object, commission: object) -> dict[str, Any]:
    """The concrete document a run is charged by. Strings, as every stored cost is: JSON has only
    doubles, and a spread is an exact tick count."""
    return {
        "type": "spread_commission",
        "spread_points": None if spread is None else str(spread),
        "commission_per_unit": None if commission is None else str(commission),
    }


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
        .options(
            selectinload(Backtest.metrics).options(
                defer(BacktestMetrics.equity_curve), defer(BacktestMetrics.targets)
            )
        )
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


@router.post(
    "/sweeps/{sweep_id}/holdout",
    response_model=CreatedSweep,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
async def create_holdout(
    sweep_id: uuid.UUID, request: CreateHoldout, session: SessionDep, queue: QueueDep
) -> CreatedSweep:
    """Run a sweep's best points again, on a window none of them was chosen on (24/09).

    The test is a sweep of its own — the chosen points on the reserved window, linked back by
    `holdout_of` — so it is queued, run, read and exported by everything a sweep already has, and
    compared by `GET /sweeps/{id}/holdout`. Chosen per (entry, chart, market), among the finished
    runs past the chart's trade floor, by `request.metric` (`holdout.choose`).

    ⚠️ **Refused, not warned:** a window that shares a bar with the one searched, and a test of a
    test — its points would be chosen on the reserved window itself.
    """
    parent = session.get(Sweep, sweep_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    if parent.holdout_of is not None or parent.holdout_rule is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="this sweep is itself a reserved-window test; test the sweep it came from",
        )
    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )
    if overlaps(request.date_from, request.date_to, parent.date_from, parent.date_to):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "the test window shares bars with the searched one "
                f"({parent.date_from:%Y-%m-%d} to {parent.date_to:%Y-%m-%d}): it would not be "
                "out of sample"
            ),
        )

    own, _followers = _points_of(parent)
    runs_of: dict[int, tuple[Backtest, str]] = {}
    candidates: list[Candidate] = []
    for order, (run, symbol) in enumerate(_runs_of(session, parent.id, done_only=True)):
        point = own.get(str(run.strategy_id))
        if point is None or run.metrics is None:
            continue
        runs_of[order] = (run, symbol)
        candidates.append(
            Candidate(
                group=(str(point["entry_id"]), run.timeframe, symbol),
                order=order,
                metrics=run.metrics,
            )
        )
    floors = {**MIN_TRADES, **request.min_trades}
    bounds = Bounds(
        max_drawdown_r=request.max_drawdown_r,
        min_positive_year_share=request.min_positive_year_share,
    )
    chosen = choose(
        candidates, metric=request.metric, top_n=request.top_n, floors=floors, bounds=bounds
    )
    if not chosen:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no finished run of this sweep can be ranked: none passed its chart's trade "
            f"floor with a {request.metric.value} to rank by"
            + (
                ""
                if bounds == Bounds()
                else " within the drawdown and positive-year limits (a run recorded before "
                "25/09 has no risk in R and never passes them)"
            ),
        )

    picked = [runs_of[one.order] for one in chosen]
    symbols = [one for one in parent.symbols if any(symbol == one for _run, symbol in picked)]
    timeframes = [
        one for one in parent.timeframes if any(run.timeframe == one for run, _ in picked)
    ]
    uncovered = uncovered_markets(session, symbols, timeframes, request.date_from, request.date_to)
    empty = {(market.symbol, market.timeframe) for market in uncovered}
    runnable = [(run, symbol) for run, symbol in picked if (symbol, run.timeframe) not in empty]
    if not runnable:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="no candles in this window for: " + ", ".join(map(describe, uncovered)),
        )

    found = {
        instrument.symbol: instrument
        for instrument in session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols)))
    }
    tested = {str(run.strategy_id) for run, _symbol in runnable}
    holdout = Sweep(
        entry_ids=list(parent.entry_ids),
        symbols=symbols,
        timeframes=timeframes,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=parent.initial_capital,
        skipped=[market.model_dump() for market in uncovered],
        # The parent's own point records, copied: the same strategies at the same coordinates —
        # the one thing this sweep changes is the window.
        points=[point for key, point in own.items() if key in tested],
        holdout_of=parent.id,
        holdout_rule={
            "metric": request.metric.value,
            "top_n": request.top_n,
            "min_trades": {one: max(floors.get(one, 0), 1) for one in timeframes},
            # Only when asked: a rule that lists no limit is a test that set none.
            **(
                {}
                if request.max_drawdown_r is None
                else {"max_drawdown_r": str(request.max_drawdown_r)}
            ),
            **(
                {}
                if request.min_positive_year_share is None
                else {"min_positive_year_share": str(request.min_positive_year_share)}
            ),
        },
    )
    session.add(holdout)
    runs = [
        Backtest(
            sweep=holdout,
            strategy_id=run.strategy_id,
            instrument_id=found[symbol].id,
            timeframe=run.timeframe,
            date_from=request.date_from,
            date_to=request.date_to,
            initial_capital=run.initial_capital,
            # Charged exactly as the run it was chosen by: a test under other costs would differ
            # from its in-sample side in the one respect the comparison is about.
            cost_model=dict(run.cost_model),
            status=BacktestStatus.QUEUED,
            engine_version=ENGINE_VERSION,
        )
        for run, symbol in runnable
    ]
    session.add_all(runs)
    session.commit()
    # After the commit, and with the run's own id as the job id — the sweep launch's reasons.
    for one in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(one.id), _job_id=str(one.id))
    return CreatedSweep(id=holdout.id, runs=len(runs), skipped=uncovered)


def _runs_of(
    session: Session, sweep_id: uuid.UUID, *, done_only: bool = False
) -> list[tuple[Backtest, str]]:
    """A sweep's runs with their market, in launch order, metrics loaded without the heavy
    columns — the curve and the ladder, which neither a choice nor a comparison reads."""
    query = (
        select(Backtest, Instrument.symbol)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id == sweep_id)
        .order_by(Backtest.created_at, Backtest.id)
        .options(
            selectinload(Backtest.metrics).options(
                defer(BacktestMetrics.equity_curve), defer(BacktestMetrics.targets)
            )
        )
    )
    if done_only:
        query = query.where(Backtest.status == BacktestStatus.DONE)
    return [(run, symbol) for run, symbol in session.execute(query).all()]


def _side(run: Backtest) -> HoldoutSide:
    metrics = run.metrics
    return HoldoutSide(
        run_id=run.id,
        status=run.status.value,
        net_return=None if metrics is None else metrics.net_profit / run.initial_capital,
        total_trades=None if metrics is None else metrics.total_trades,
        profit_factor=None if metrics is None else metrics.profit_factor,
        max_drawdown_pct=None if metrics is None else metrics.max_drawdown_pct,
    )


@router.get("/sweeps/{sweep_id}/holdout", response_model=HoldoutOut, responses=_NOT_FOUND)
def get_holdout(sweep_id: uuid.UUID, session: SessionDep) -> HoldoutOut:
    """A reserved-window test beside the runs its points were chosen by — point by point, and
    per (entry, chart). A 404 for a sweep that is not such a test."""
    holdout = session.get(Sweep, sweep_id)
    if holdout is None or holdout.holdout_rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="holdout not found")
    parent = None if holdout.holdout_of is None else session.get(Sweep, holdout.holdout_of)
    chosen_by = (
        {}
        if parent is None
        else {
            (run.strategy_id, symbol, run.timeframe): run
            for run, symbol in _runs_of(session, parent.id)
        }
    )
    own, _followers = _points_of(holdout)
    names = {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in holdout.entry_ids])
            )
        )
    }

    rows: list[HoldoutRow] = []
    for run, symbol in _runs_of(session, holdout.id):
        point = own.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        before = chosen_by.get((run.strategy_id, symbol, run.timeframe))
        rows.append(
            HoldoutRow(
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                symbol=symbol,
                timeframe=run.timeframe,
                label=str(point.get("label", "")),
                values=dict(point.get("values", {})),
                in_sample=None if before is None else _side(before),
                out_of_sample=_side(run),
            )
        )

    grouped: dict[tuple[str, str], list[HoldoutRow]] = {}
    for row in rows:
        grouped.setdefault((row.entry_id, row.timeframe), []).append(row)
    groups: list[HoldoutGroup] = []
    for (entry_id, timeframe), members in grouped.items():
        after = [
            row.out_of_sample.net_return
            for row in members
            if row.out_of_sample.status == BacktestStatus.DONE.value
            and row.out_of_sample.net_return is not None
        ]
        before_returns = [
            row.in_sample.net_return
            for row in members
            if row.in_sample is not None and row.in_sample.net_return is not None
        ]
        groups.append(
            HoldoutGroup(
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                timeframe=timeframe,
                points=len(members),
                done=len(after),
                in_sample_median_return=median_of(before_returns),
                out_of_sample_median_return=median_of(after),
                out_of_sample_positive=positive_share(after),
            )
        )

    return HoldoutOut(
        id=holdout.id,
        holdout_of=holdout.holdout_of,
        rule=dict(holdout.holdout_rule),
        date_from=holdout.date_from,
        date_to=holdout.date_to,
        searched_from=None if parent is None else parent.date_from,
        searched_to=None if parent is None else parent.date_to,
        groups=groups,
        rows=rows,
    )


def _entry_names(session: Session, sweep: Sweep) -> dict[str, str]:
    return {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in sweep.entry_ids])
            )
        )
    }


def _slicing_out(row: SweepSlicing) -> SlicingOut:
    return SlicingOut(
        id=row.id,
        sweep_id=row.sweep_id,
        mode=row.mode,
        block_trades=row.block_trades,
        pass_share=row.pass_share,
        created_at=row.created_at,
        groups=[SlicedGroup.model_validate(one) for one in row.result["groups"]],
        points=[SlicedPoint.model_validate(one) for one in row.result["points"]],
    )


def _finished_test(
    session: Session, sweep_id: uuid.UUID, *, doing: str
) -> tuple[Sweep, list[tuple[Backtest, str]]]:
    """A reserved-window test whose every run has finished, and its finished runs.

    ⚠️ **Refused for a sweep that is not such a test** — its runs were not chosen on other data,
    and analysing them would dress an in-sample result as a second opinion — **and for a test
    still running**, whose answer would silently leave out the runs not yet finished.
    """
    test = session.get(Sweep, sweep_id)
    if test is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    if test.holdout_rule is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"only a reserved-window test can be {doing}; test this sweep first",
        )
    runs = _runs_of(session, test.id)
    if any(run.status in (BacktestStatus.QUEUED, BacktestStatus.RUNNING) for run, _ in runs):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="the test is still running: wait until every run has finished",
        )
    return test, [(run, symbol) for run, symbol in runs if run.status == BacktestStatus.DONE]


def _trades_of(
    session: Session, runs: list[tuple[Backtest, str]]
) -> dict[uuid.UUID, list[ClosedTrade]]:
    """Every closed trade of `runs` in one query, in entry order, with only what slicing and
    resampling read: when it was entered and its R."""
    found: dict[uuid.UUID, list[ClosedTrade]] = {}
    for backtest_id, entry_time, r in session.execute(
        select(Trade.backtest_id, Trade.entry_time, Trade.r_multiple)
        .where(
            Trade.backtest_id.in_([run.id for run, _ in runs]),
            Trade.exit_time.is_not(None),
        )
        .order_by(Trade.backtest_id, Trade.entry_time, Trade.id)
    ):
        found.setdefault(backtest_id, []).append(ClosedTrade(entry_time=entry_time, r=r))
    return found


@router.post(
    "/sweeps/{sweep_id}/slicings",
    response_model=SlicingOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
def create_slicing(sweep_id: uuid.UUID, request: CreateSlicing, session: SessionDep) -> SlicingOut:
    """Cut a finished reserved-window test's runs by year or into blocks of trades, judge each
    point by the share of pieces that made money, and keep the answer (25/09, `slices`).

    Runs nothing: the test's runs keep their trades win or lose (`retention`). Refused for a
    sweep that is not such a test — its runs were not chosen on other data, and slicing them
    would dress an in-sample result as a second opinion — and for a test still running, whose
    verdict would silently leave out the runs not yet finished.
    """
    test, done = _finished_test(session, sweep_id, doing="judged in pieces")
    trades_of = _trades_of(session, done)

    own, _followers = _points_of(test)
    names = _entry_names(session, test)
    points: list[SlicedPoint] = []
    for run, symbol in done:
        point = own.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        kept = run.recorded is not Recorded.METRICS
        trades = trades_of.get(run.id, [])
        pieces = (
            []
            if not kept
            else by_year(trades, test.date_from, test.date_to)
            if request.mode is SliceMode.CALENDAR
            else by_blocks(trades, request.block_trades or 0)
        )
        judged = verdict(pieces, request.pass_share)
        points.append(
            SlicedPoint(
                run_id=run.id,
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                symbol=symbol,
                timeframe=run.timeframe,
                label=str(point.get("label", "")),
                trades_kept=kept,
                unscored=sum(1 for one in trades if one.r is None),
                net_r=sum((one.r for one in trades if one.r is not None), Decimal(0)),
                slices=[
                    SliceOut(
                        label=one.label,
                        date_from=one.date_from,
                        date_to=one.date_to,
                        trades=one.trades,
                        net_r=one.net_r,
                        counted=one.counted,
                    )
                    for one in pieces
                ],
                counted=judged.counted,
                positive=judged.positive,
                share=judged.share,
                passed=judged.passed,
            )
        )

    grouped: dict[tuple[str, str], list[SlicedPoint]] = {}
    for one in points:
        grouped.setdefault((one.entry_id, one.timeframe), []).append(one)
    groups = [
        SlicedGroup(
            entry_id=entry_id,
            entry_name=names.get(entry_id),
            timeframe=timeframe,
            points=len(members),
            judged=sum(1 for one in members if one.trades_kept),
            passed=sum(1 for one in members if one.passed),
        )
        for (entry_id, timeframe), members in grouped.items()
    ]

    row = SweepSlicing(
        sweep_id=test.id,
        mode=request.mode,
        block_trades=request.block_trades,
        pass_share=request.pass_share,
        result={
            "groups": [one.model_dump(mode="json") for one in groups],
            "points": [one.model_dump(mode="json") for one in points],
        },
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _slicing_out(row)


@router.get("/sweeps/{sweep_id}/slicings", response_model=list[SlicingOut], responses=_NOT_FOUND)
def list_slicings(sweep_id: uuid.UUID, session: SessionDep) -> list[SlicingOut]:
    """Every slicing kept for this test, newest first."""
    if session.get(Sweep, sweep_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    rows = session.scalars(
        select(SweepSlicing)
        .where(SweepSlicing.sweep_id == sweep_id)
        .order_by(SweepSlicing.created_at.desc(), SweepSlicing.id)
    )
    return [_slicing_out(row) for row in rows]


def _montecarlo_out(row: SweepMonteCarlo) -> MonteCarloOut:
    return MonteCarloOut(
        id=row.id,
        sweep_id=row.sweep_id,
        paths=row.paths,
        seed=row.seed,
        created_at=row.created_at,
        points=[MonteCarloPoint.model_validate(one) for one in row.result["points"]],
    )


def _spread_out(spread: Spread) -> SpreadOut:
    return SpreadOut(p5=spread.p5, p50=spread.p50, p95=spread.p95, p99=spread.p99)


@router.post(
    "/sweeps/{sweep_id}/montecarlos",
    response_model=MonteCarloOut,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
def create_montecarlo(
    sweep_id: uuid.UUID, request: CreateMonteCarlo, session: SessionDep
) -> MonteCarloOut:
    """Resample every point of a finished reserved-window test and keep the answer (25/09).

    Each point's trades are drawn with replacement into `paths` paths (`montecarlo.simulate`),
    from a generator seeded by the request's seed and the run's id: repeatable, and no point's
    draws depend on another's. Runs nothing — the test keeps its trades — but takes seconds on a
    large test, which is why it is started by hand and kept.
    """
    test, done = _finished_test(session, sweep_id, doing="resampled")
    trades_of = _trades_of(session, done)
    seed = request.seed or secrets.token_hex(8)
    own, _followers = _points_of(test)
    names = _entry_names(session, test)

    points: list[MonteCarloPoint] = []
    for run, symbol in done:
        point = own.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        kept = run.recorded is not Recorded.METRICS
        rs = [one.r for one in trades_of.get(run.id, []) if one.r is not None]
        seen = observed(rs)
        simulated = simulate(rs, paths=request.paths, seed=f"{seed}:{run.id}") if kept else None
        points.append(
            MonteCarloPoint(
                run_id=run.id,
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                symbol=symbol,
                timeframe=run.timeframe,
                label=str(point.get("label", "")),
                trades_kept=kept,
                trades=len(rs),
                observed_net_r=seen.net_r,
                observed_drawdown_r=seen.drawdown_r,
                observed_losing_streak=seen.losing_streak,
                simulated=None
                if simulated is None
                else SimulatedOut(
                    paths=simulated.paths,
                    trades=simulated.trades,
                    drawdown_r=_spread_out(simulated.drawdown_r),
                    losing_streak=_spread_out(simulated.losing_streak),
                    net_r=_spread_out(simulated.net_r),
                    negative_share=simulated.negative_share,
                ),
            )
        )

    row = SweepMonteCarlo(
        sweep_id=test.id,
        paths=request.paths,
        seed=seed,
        result={"points": [one.model_dump(mode="json") for one in points]},
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return _montecarlo_out(row)


@router.get(
    "/sweeps/{sweep_id}/montecarlos", response_model=list[MonteCarloOut], responses=_NOT_FOUND
)
def list_montecarlos(sweep_id: uuid.UUID, session: SessionDep) -> list[MonteCarloOut]:
    """Every resampling kept for this test, newest first."""
    if session.get(Sweep, sweep_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    rows = session.scalars(
        select(SweepMonteCarlo)
        .where(SweepMonteCarlo.sweep_id == sweep_id)
        .order_by(SweepMonteCarlo.created_at.desc(), SweepMonteCarlo.id)
    )
    return [_montecarlo_out(row) for row in rows]


@router.get("/sweeps/{sweep_id}", response_model=SweepOut, responses=_NOT_FOUND)
def get_sweep(
    sweep_id: uuid.UUID,
    session: SessionDep,
    runs: Annotated[Literal["all", "none"], Query()] = "all",
) -> SweepOut:
    """A sweep read back: the question that was asked, and every run it became.

    ⚠️ **`runs=none` is what a screen polls.** A sweep of 22 thousand runs answered with all of them
    is 89 MB, and the screen asked every three seconds while it ran (measured 24/09). Without the
    runs the body carries the header, each entry's summary and `counts`; a screen reads the runs a
    page at a time from `GET /sweeps/{id}/runs`. `all` stays the default for every other reader.

    ⚠️ **The coordinates come from `sweeps.points`, never from the strategy's name.** The name
    carries the label (`9.1 sem filtro [M15 · period=5]`) because that is what a run log row
    shows — but it is a caption, and recovering axis values by splitting one works until an
    entry's name is a prefix of another's or a value contains the separator. The first draft of
    this endpoint did exactly that; the column exists because of it.
    """
    sweep, entries, coordinates, followers, rows = _read_sweep(session, sweep_id)

    out: list[SweepRunOut] = []
    scored: dict[str, list[tuple[Backtest, str]]] = {one: [] for one in sweep.entry_ids}
    for run, strategy, instrument in rows:
        point = coordinates.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        label = str(point.get("label", ""))
        if runs == "all":
            out.append(
                _run_out((run, strategy, instrument), point, entries=entries, followers=followers)
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
            targets=rungs_across(scored[entry_id]),
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
        holdout_of=sweep.holdout_of,
        holdout_rule=sweep.holdout_rule,
        counts=_counts(run for run, _strategy, _instrument in rows),
        entries=summaries,
        runs=out,
        skipped=[UncoveredMarket.model_validate(one) for one in sweep.skipped],
        failed_collections=failed_collections(session, Backtest.sweep_id == sweep.id),
    )


_SweepRows = list[tuple[Backtest, Strategy, Instrument]]
_Point = dict[str, Any]


def _run_out(
    row: tuple[Backtest, Strategy, Instrument],
    point: _Point,
    *,
    entries: dict[str, CatalogEntry],
    followers: dict[str, list[_Point]],
) -> SweepRunOut:
    """One run of a sweep with its coordinates, as every view of the runs shows it."""
    run, strategy, instrument = row
    entry_id = str(point.get("entry_id", ""))
    entry = entries.get(entry_id)
    return SweepRunOut(
        entry_id=uuid.UUID(entry_id) if entry_id else uuid.UUID(int=0),
        # ⚠️ The shelf label if the entry is still there, and the document's own name if it is
        # not. Removing an entry is allowed by design and must not blank a finished sweep — what
        # it costs is the label, never the measurement.
        entry_name=entry.name if entry is not None else strategy.name,
        label=str(point.get("label", "")),
        values=dict(point.get("values", {})),
        run=list_item(run, instrument.symbol, strategy.name, strategy.version),
        equivalents=[
            SweepPoint(label=str(one["label"]), values=dict(one["values"]))
            for one in followers.get(str(run.strategy_id), [])
        ],
    )


def _counts(runs: Iterable[Backtest]) -> SweepRunCounts:
    counted = dict.fromkeys(BacktestStatus, 0)
    for run in runs:
        counted[run.status] += 1
    return SweepRunCounts(
        total=sum(counted.values()),
        done=counted[BacktestStatus.DONE],
        running=counted[BacktestStatus.RUNNING],
        queued=counted[BacktestStatus.QUEUED],
        failed=counted[BacktestStatus.FAILED],
    )


RankBy = Literal[
    "return",
    "profit_factor",
    "win_rate",
    "expectancy",
    "drawdown",
    "net_r",
    "recovery_r",
    "positive_years",
    "drawdown_r",
]


def _recovery_r() -> tuple[ColumnElement[Any], ...]:
    """`holdout.recovery_r` in SQL: a gain with no drawdown is the best there is, no drawdown and
    no gain says nothing, and a run recorded before R existed has no score."""
    metrics = BacktestMetrics
    tier = case(
        (or_(metrics.net_r.is_(None), metrics.max_drawdown_r.is_(None)), 2),
        (and_(metrics.max_drawdown_r == 0, metrics.net_r > 0), 0),
        (metrics.max_drawdown_r == 0, 2),
        else_=1,
    )
    return (tier, (metrics.net_r / func.nullif(metrics.max_drawdown_r, 0)).desc())


def _unless_null(
    column: InstrumentedAttribute[Any], *, smallest_first: bool = False
) -> tuple[ColumnElement[Any], ...]:
    """A column best first — the smallest, when that is better — with its nulls last."""
    tier = case((column.is_(None), 2), else_=1)
    return (tier, column.asc() if smallest_first else column.desc())


_RANKINGS: dict[str, Callable[[], tuple[ColumnElement[Any], ...]]] = {
    "return": lambda: _unless_null(BacktestMetrics.net_profit),
    "profit_factor": lambda: (
        case(
            (and_(BacktestMetrics.profit_factor.is_(None), BacktestMetrics.gross_profit > 0), 0),
            (BacktestMetrics.profit_factor.is_not(None), 1),
            else_=2,
        ),
        BacktestMetrics.profit_factor.desc(),
    ),
    "win_rate": lambda: (
        case(
            (
                and_(BacktestMetrics.total_trades.is_not(None), BacktestMetrics.total_trades > 0),
                1,
            ),
            else_=2,
        ),
        BacktestMetrics.win_rate.desc(),
    ),
    "expectancy": lambda: _unless_null(BacktestMetrics.expectancy),
    "drawdown": lambda: _unless_null(BacktestMetrics.max_drawdown_pct, smallest_first=True),
    "net_r": lambda: _unless_null(BacktestMetrics.net_r),
    "recovery_r": _recovery_r,
    "positive_years": lambda: _unless_null(BacktestMetrics.positive_year_share),
    "drawdown_r": lambda: _unless_null(BacktestMetrics.max_drawdown_r, smallest_first=True),
}


def _ranked(rank_by: RankBy) -> tuple[ColumnElement[Any], ...]:
    """How `GET /sweeps/{id}/runs` orders: best first, nothing to rank by last, ties by launch.

    The screen's own rule (`sweep/ranking.ts`), moved to where the runs are: a profit factor with
    gains and no loss is the best there is, not a blank; a win rate over no trade is not a
    measurement; the smallest drawdown ranks first; an unfinished run has no score at all. The
    measures in R (25/09) are null for a run recorded before them, and rank it last.
    """
    return _RANKINGS[rank_by]()


@router.get("/sweeps/{sweep_id}/runs", response_model=SweepRunsPage, responses=_NOT_FOUND)
def get_sweep_runs(  # noqa: PLR0913 — one query parameter per thing a page is asked by
    sweep_id: uuid.UUID,
    session: SessionDep,
    *,
    entry_id: uuid.UUID | None = None,
    rank_by: RankBy = "return",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> SweepRunsPage:
    """One page of a sweep's runs — of one entry, when named — best first by `rank_by`.

    Ranked by the database rather than by the screen (24/09): the screen used to hold every run and
    sort them itself, which on a sweep of 22 thousand runs meant 89 MB per poll.
    """
    sweep = session.get(Sweep, sweep_id)
    if sweep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    own, followers = _points_of(sweep)
    strategies = [
        uuid.UUID(key)
        for key, point in own.items()
        if entry_id is None or str(point.get("entry_id")) == str(entry_id)
    ]
    where = (Backtest.sweep_id == sweep.id, Backtest.strategy_id.in_(strategies))
    total = session.scalar(select(func.count()).select_from(Backtest).where(*where)) or 0
    rows = session.execute(
        select(Backtest, Strategy, Instrument)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .outerjoin(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .where(*where)
        # Ties keep the order `_read_sweep` reads in, which is the order the screen ranked in.
        .order_by(*_ranked(rank_by), Backtest.created_at, Strategy.name, Instrument.symbol)
        .offset(offset)
        .limit(limit)
        .options(
            selectinload(Backtest.metrics).options(
                defer(BacktestMetrics.equity_curve), defer(BacktestMetrics.targets)
            )
        )
    ).all()
    entries = {
        str(entry.id): entry
        for entry in session.scalars(
            select(CatalogEntry).where(CatalogEntry.id.in_([uuid.UUID(x) for x in sweep.entry_ids]))
        )
    }
    return SweepRunsPage(
        total=total,
        offset=offset,
        limit=limit,
        items=[
            _run_out(
                (run, strategy, instrument),
                own.get(str(run.strategy_id), {}),
                entries=entries,
                followers=followers,
            )
            for run, strategy, instrument in rows
        ],
    )


def _points_of(sweep: Sweep) -> tuple[dict[str, _Point], dict[str, list[_Point]]]:
    """A sweep's points by strategy: the one each run is, and the ones it answers besides.

    ⚠️ **Split on `same_as`, never on order.** A point answered by another's run carries that
    run's strategy id (`create_sweep`), so a plain `{strategy_id: point}` would keep whichever came
    last — and a run would be shown at a follower's coordinates rather than its own.
    """
    own: dict[str, _Point] = {}
    followers: dict[str, list[_Point]] = {}
    for point in sweep.points:
        key = str(point["strategy_id"])
        if point.get("same_as") is None:
            own[key] = point
        else:
            followers.setdefault(key, []).append(point)
    return own, followers


def _read_sweep(
    session: SessionDep, sweep_id: uuid.UUID
) -> tuple[Sweep, dict[str, CatalogEntry], dict[str, _Point], dict[str, list[_Point]], _SweepRows]:
    """A sweep, its surviving shelf entries, its coordinates by strategy — each run's own point,
    and the points it answers besides — and every run joined.

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
    coordinates, followers = _points_of(sweep)

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
        followers,
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
    sweep, entries, coordinates, followers, rows = _read_sweep(session, sweep_id)
    out: list[DatasetRun] = []
    for run, strategy, instrument in rows:
        point = coordinates.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        shelved = entries.get(entry_id)
        # ⚠️ **A row for every point, the ones a run answers besides its own included** (his
        # answer, 24/09) — each at its own coordinates, each saying whose run it is.
        for one in [point, *followers.get(str(run.strategy_id), [])]:
            same_as = one.get("same_as")
            out.append(
                DatasetRun(
                    sweep_id=str(sweep.id),
                    run=run,
                    entry_id=entry_id,
                    entry_name=None if shelved is None else shelved.name,
                    strategy_version=strategy.version,
                    symbol=instrument.symbol,
                    asset_class=str(instrument.asset_class),
                    values=dict(one.get("values", {})),
                    same_as=None if same_as is None else str(same_as),
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
