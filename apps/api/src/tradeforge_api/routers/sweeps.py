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

import datetime as dt
import secrets
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy import ColumnElement, Text, and_, case, cast, func, insert, literal, or_, select
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
from tradeforge_api.routers.studies import aggregate_scored, strategies_for
from tradeforge_api.runner import ENGINE_VERSION, build_cost_model, swap_rates
from tradeforge_api.schemas import (
    CombineSweeps,
    CreatedSweep,
    CreateHoldout,
    CreateMonteCarlo,
    CreateSlicing,
    CreateSweep,
    DatasetColumnOut,
    DatasetDictionaryOut,
    DatasetOmissionOut,
    HoldoutGroup,
    HoldoutOut,
    HoldoutRow,
    HoldoutSide,
    MonteCarloOut,
    MonteCarloPoint,
    PlannedCollection,
    PreviewSweepRequest,
    RefusalGroup,
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
    behaviour_key,
    iter_documents_for,
    points_in,
    size_refusal,
)
from tradeforge_api.sweep_dataset import CAVEATS, OMITTED, ROW, DatasetRun, columns_for, to_csv
from tradeforge_api.targets import rungs_of
from tradeforge_collector.collect import year_slices
from tradeforge_db.base import Base
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
from tradeforge_db.models import SweepPoint as PointRow
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


def _streams(
    pairs: list[tuple[CatalogEntry, Strategy]], timeframes: list[str]
) -> list[Iterator[SweepDocument]]:
    """Every entry's documents, one stream each — or a 422 naming the entry that cannot expand.

    ⚠️ **Every grid is checked here, before any document is built** (`iter_documents_for` raises
    at the call). A launch writes in blocks as it reads the streams, and an entry refused halfway
    through them would leave the checks of the ones after it to a transaction already writing.
    """
    out: list[Iterator[SweepDocument]] = []
    for entry, strategy in pairs:
        try:
            out.append(
                iter_documents_for(
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


def _markets_by_chart(
    symbols: list[str], timeframes: list[str], empty: set[tuple[str, str]]
) -> dict[str, list[str]]:
    """The markets each chart runs on: all of them, less the pairs with nothing to read.

    ⚠️ **Skipped by pair, not by market.** A market can hold M15 and not H4, and dropping it from
    every chart for the want of one would leave out runs that could have read every bar.
    """
    return {
        timeframe: [symbol for symbol in symbols if (symbol, timeframe) not in empty]
        for timeframe in timeframes
    }


def _worth_naming(uncovered: list[UncoveredMarket], charts: set[str]) -> list[UncoveredMarket]:
    """The uncovered pairs on a chart where some point can run — the ones that are a hole.

    ⚠️ **A chart the DSL refused everywhere is not missing data.** An H4 filter cannot run on H4,
    so EURUSD H4 without candles has no run to lose: naming it as skipped would offer a download
    that produces nothing, and would blur the two absences `Sweep.skipped` exists to keep apart.
    That chart is reported where it belongs, as the preview's refusals.
    """
    return [market for market in uncovered if market.timeframe in charts]


def _nothing_to_read(runnable: int, runs: int, skipped: list[UncoveredMarket]) -> str | None:
    """Why a sweep whose every runnable point landed on a skipped pair cannot be launched.

    ⚠️ **Asked before the size rule, and only when there were points to run.** With every pair
    skipped the sweep is empty, and the size rule's "no combination in this sweep can run" would
    blame the grid for what is missing data. With no runnable point at all, the grid *is* to
    blame, and the size rule says so.
    """
    if runnable and not runs:
        return "no candles in this window for: " + ", ".join(map(describe, skipped))
    return None


REFUSAL_EXAMPLES = 3
"""How many labels a preview names under each reason a combination was refused for."""


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

    ⚠️ **Counted as the grid streams past, never held** (26/09). A grid of 435 thousand points per
    chart took the API past 18 GB when every document was built first, and its refusals — one item
    per point — made a 1.09 GB answer. Each document is built, judged and dropped; what is kept is
    one 16-byte key per distinct behaviour and the refusals grouped by reason.
    """
    pairs = _entries(session, request.entry_ids)

    # ⚠️ **The same guard the launch has, and the preview needs it more.** Without it a
    # backwards window is not a window any dataset can overlap, so `uncovered_markets` can name
    # a market that is collected and the screen reads "no candles in this window; move the
    # window or collect them first" — blaming the data for a typo, and sending a person to run
    # a backfill they do not need. Refused the same way and in the same words the launch refuses
    # it, so one request cannot get two verdicts from the two endpoints.
    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    timeframes = list(request.timeframes)
    symbols = list(request.symbols)
    streams = _streams(pairs, timeframes)
    uncovered_all = uncovered_markets(
        session, symbols, timeframes, request.date_from, request.date_to
    )
    # ⚠️ Counted as the launch counts them when told not to collect: the pairs with no candles
    # are skipped, not refused (his rule, 18/09), so they subtract rather than block.
    markets = _markets_by_chart(
        symbols, timeframes, {(market.symbol, market.timeframe) for market in uncovered_all}
    )

    documents = 0
    answered = 0
    owners: set[bytes] = set()
    charts: set[str] = set()
    runs_by_chart: dict[str, int] = dict.fromkeys(timeframes, 0)
    per_entry: list[SweepEntryPreview] = []
    for (entry, _strategy), stream in zip(pairs, streams, strict=True):
        groups: dict[str, RefusalGroup] = {}
        for doc in stream:
            documents += 1
            reason = refusal_of(dict(doc.document))
            if reason is not None:
                group = groups.setdefault(reason, RefusalGroup(reason=reason, count=0, examples=[]))
                group.count += 1
                if len(group.examples) < REFUSAL_EXAMPLES:
                    group.examples.append(doc.label)
                continue
            charts.add(doc.timeframe)
            # ⚠️ Counted as the launch counts them: a point another point answers runs nothing
            # (24/09). The same key, in the same order, so the two agree on which one runs.
            key = behaviour_key(doc, unread_params)
            if key in owners:
                answered += 1
                continue
            owners.add(key)
            runs_by_chart[doc.timeframe] += len(markets[doc.timeframe])
        per_entry.append(
            SweepEntryPreview(
                entry_id=entry.id,
                name=entry.name,
                points=points_in(entry.grid),
                refusals=list(groups.values()),
            )
        )

    uncovered = _worth_naming(uncovered_all, charts)
    runs = sum(runs_by_chart.values())
    # ⚠️ The empty sweep and the all-skipped one are asked of the same functions the launch asks,
    # so the two endpoints cannot answer this in different words. Reported as an `error` rather
    # than raised, which is the one thing that *is* different: a preview that raised would have
    # nothing to preview.
    error = _nothing_to_read(len(owners), runs, uncovered) or size_refusal(runs)

    return SweepPreview(
        runs=runs,
        documents=documents,
        shared=answered,
        entries=per_entry,
        uncovered=uncovered,
        # Every run of a sweep reads the same window, so what varies from run to run is the
        # chart: the bars in the window depend on the document's timeframe, not on the market.
        backtest_time=backtests_time(
            session,
            (
                (request.date_from, request.date_to, timeframe)
                for timeframe, count in runs_by_chart.items()
                for _ in range(count)
            ),
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

    **All or nothing in the face of a refusal.** Nothing is committed until every refusal has
    been decided: a sweep that half-exists is worse than one that was refused, because the caller
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
    sweep, run_ids, collections, shared_count, skipped = launch_sweep(session, request)

    # ⚠️ After the commit, and with the run's own id as the job id. A worker is fast enough to
    # claim a job before an uncommitted row is visible; and the derived job id makes the enqueue
    # idempotent, so a crash halfway through this loop is recovered by re-sending rather than by
    # working out which of two thousand runs were reached.
    # ⚠️ **That claim is the runs' alone.** The downloads carry no job id, as the basket's do not:
    # re-sending them queues each window again, and a crash before one is sent leaves it
    # `queued` with its runs deferring until the queue has been silent for `WAIT_LIMIT`.
    for collection in collections:
        await queue.enqueue_job(COLLECT_RANGE, str(collection.id), _queue_name=COLLECT_QUEUE)
    for run_id in run_ids:
        await queue.enqueue_job(RUN_BACKTEST, str(run_id), _job_id=str(run_id))

    return CreatedSweep(id=sweep.id, runs=len(run_ids), shared=shared_count, skipped=skipped)


LAUNCH_BLOCK = 2000
"""Runnable points a launch writes at a time — their strategies, their points and their runs.

The memory a launch holds is this block plus one 16-byte key and one id per distinct behaviour,
whatever the grid's size (26/09)."""


def launch_sweep(
    session: Session, request: CreateSweep, *, template_id: uuid.UUID | None = None
) -> tuple[Sweep, list[uuid.UUID], list[Collection], int, list[UncoveredMarket]]:
    """Decide, write and commit a sweep — the endpoint's body, shared with a template's queue
    (`sweep_templates`, 26/09), which launches one market at a time. Returns the ids of the runs
    to queue; the caller queues them and the collections. Refusals are `HTTPException`s with the
    sentence a person reads, raised before the commit — nothing of a refused sweep is kept.

    ⚠️ **Written in blocks as the grid streams past, committed once** (26/09). A grid of 435
    thousand points per chart built whole took the API past 18 GB; here each block of
    `LAUNCH_BLOCK` points is flushed and dropped. Postgres holds the flushed rows inside the one
    transaction, so a refusal found at the end — every point skipped, say — still leaves nothing.
    """
    timeframes = list(request.timeframes)
    symbols = list(request.symbols)

    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )
    pairs = _entries(session, request.entry_ids)

    found = {
        instrument.symbol: instrument
        for instrument in session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols)))
    }
    unknown = [symbol for symbol in symbols if symbol not in found]
    if unknown:
        # Every bad symbol at once. A caller fixing a typo list one round trip at a time is a
        # caller the API is failing — the same answer `/baskets` gives.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown symbols: {', '.join(unknown)}",
        )
    costs = _costs_for(request.cost_model, found)
    streams = _streams(pairs, timeframes)

    planned = (
        to_collect(
            session,
            symbols=symbols,
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
    uncovered = [
        market
        for market in uncovered_markets(
            session, symbols, timeframes, request.date_from, request.date_to
        )
        if (market.symbol, market.timeframe) not in collectable
    ]
    markets = _markets_by_chart(
        symbols, timeframes, {(market.symbol, market.timeframe) for market in uncovered}
    )

    sweep = Sweep(
        entry_ids=[str(one) for one in request.entry_ids],
        symbols=symbols,
        timeframes=timeframes,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
        skipped=[],
        template_id=template_id,
    )
    session.add(sweep)
    session.flush()

    writer = _SweepWriter(
        session,
        sweep=sweep,
        request=request,
        markets=markets,
        found=found,
        costs=costs,
        collectable=collectable,
    )
    for stream in streams:
        for doc in stream:
            # ⚠️ Refused points are not part of the space: the preview names them, and none is
            # enqueued to fail (18/09).
            if refusal_of(dict(doc.document)) is None:
                writer.add(doc)
    writer.close()

    skipped = _worth_naming(uncovered, writer.charts)
    # The same questions the preview asked, of the same functions, so the words a person read on
    # the screen are the words they get back from the button.
    refusal = _nothing_to_read(writer.runnable, len(writer.run_ids), skipped) or size_refusal(
        len(writer.run_ids)
    )
    if refusal is not None:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refusal)
    sweep.skipped = [market.model_dump() for market in skipped]

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

    return sweep, writer.run_ids, writer.collections, writer.followers, skipped


INSERT_ROWS = 500
"""Rows a single `INSERT ... VALUES` of a launch carries — under Postgres's 65 535 parameters
for the widest row written (`backtests`, thirteen columns)."""


def _insert(session: Session, model: type[Base], rows: list[dict[str, Any]]) -> None:
    """`rows` into `model`'s table, `INSERT_ROWS` a statement.

    ⚠️ **Many rows a statement, spelled out** (26/09). Handed to `execute` as an executemany,
    the points of a 41 thousand-point launch went as 1 407 statements and took 62 of its 72
    seconds. Dicts, never objects the session would keep for the length of the transaction.
    """
    for start in range(0, len(rows), INSERT_ROWS):
        session.execute(insert(model).values(rows[start : start + INSERT_ROWS]))


class _SweepWriter:
    """A launch's runnable points, written a block at a time: strategies, points, runs, links.

    ⚠️ **One run for points that run the same** (his answer, 24/09): a point that differs from an
    earlier one only in a parameter its entry point never reads is answered by that one's run. It
    is still a point of the sweep — a row of `sweep_points` carrying the answering strategy and
    `same_as` — and never a run of its own. The first point of each behaviour, in launch order,
    runs: the order the preview counts in, so the two agree on which one.

    ⚠️ **A point whose chart runs on no market is not written**, and nor is a point it answers: a
    chart skipped on every market leaves documents no run reads, and writing them would put
    points on the map with nothing measured under them.
    """

    def __init__(  # noqa: PLR0913 — the launch's decided context, passed once
        self,
        session: Session,
        *,
        sweep: Sweep,
        request: CreateSweep,
        markets: dict[str, list[str]],
        found: dict[str, Instrument],
        costs: dict[str, dict[str, Any]],
        collectable: dict[tuple[str, str], PlannedCollection],
    ) -> None:
        self._session = session
        self._sweep = sweep
        self._request = request
        self._markets = markets
        self._found = found
        self._costs = costs
        self._collectable = collectable
        # Per behaviour: the strategy and label of the point that runs it, or `None` when that
        # point runs on no market. Only keys and ids — never a document.
        self._owners: dict[bytes, tuple[uuid.UUID, str] | None] = {}
        self._block: list[tuple[SweepDocument, bytes, bool]] = []
        self._position = 0
        self._downloads: dict[tuple[str, str], list[Collection]] = {}
        self.run_ids: list[uuid.UUID] = []
        self.collections: list[Collection] = []
        self.charts: set[str] = set()
        self.runnable = 0
        """Points that run themselves, on a market or not — `_nothing_to_read`'s question."""
        self.followers = 0
        """Points written that another point's run answers."""
        self._pending: set[bytes] = set()

    def add(self, doc: SweepDocument) -> None:
        """One runnable point, in launch order."""
        self.charts.add(doc.timeframe)
        key = behaviour_key(doc, unread_params)
        runs_itself = key not in self._owners and key not in self._pending
        if runs_itself:
            self.runnable += 1
            self._pending.add(key)
        self._block.append((doc, key, runs_itself))
        if len(self._block) >= LAUNCH_BLOCK:
            self._write()

    def close(self) -> None:
        """Write what is left of the last block."""
        if self._block:
            self._write()

    def _write(self) -> None:
        block, self._block = self._block, []
        running = [
            doc for doc, _key, runs_itself in block if runs_itself and self._markets[doc.timeframe]
        ]
        # ⚠️ **Reused, never reimplemented.** `strategies_for` is where the reuse-or-version
        # decision lives — a point's document is written once and a later sweep finds it rather
        # than colliding on `(name, version)`.
        strategies = strategies_for(
            self._session,
            [GridPoint(values=dict(doc.values), document=dict(doc.document)) for doc in running],
        )
        self._session.add_all(strategies)
        self._session.flush()
        strategy_of = {
            id(doc): strategy.id for doc, strategy in zip(running, strategies, strict=True)
        }

        points: list[dict[str, Any]] = []
        runs: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        for doc, key, runs_itself in block:
            if runs_itself:
                self._pending.discard(key)
                strategy_id = strategy_of.get(id(doc))
                self._owners[key] = None if strategy_id is None else (strategy_id, doc.label)
                if strategy_id is None:
                    continue
                points.append(self._point(doc, strategy_id, None))
                for symbol in self._markets[doc.timeframe]:
                    run_id = uuid.uuid4()
                    runs.append(self._run(run_id, doc, strategy_id, symbol))
                    links += self._links(run_id, symbol, doc.timeframe)
                continue
            owner = self._owners[key]
            if owner is None:
                continue
            owner_strategy, owner_label = owner
            points.append(self._point(doc, owner_strategy, owner_label))
            self.followers += 1

        _insert(self._session, PointRow, points)
        _insert(self._session, Backtest, runs)
        self.run_ids += [run["id"] for run in runs]
        _insert(self._session, BacktestCollection, links)

    def _point(
        self, doc: SweepDocument, strategy_id: uuid.UUID, same_as: str | None
    ) -> dict[str, Any]:
        # ⚠️ **The coordinates are written down now, keyed by the strategy they produced.** The
        # alternative is recovering them later from the document's name — and a name is a
        # caption: `{entry} [{label}]` splits cleanly until one entry's name is a prefix of
        # another's, or a value contains the separator.
        row = {
            "sweep_id": self._sweep.id,
            "position": self._position,
            "strategy_id": strategy_id,
            "entry_id": doc.entry_id,
            "label": doc.label,
            "coordinates": dict(doc.values),
            "same_as": same_as,
        }
        self._position += 1
        return row

    def _run(
        self, run_id: uuid.UUID, doc: SweepDocument, strategy_id: uuid.UUID, symbol: str
    ) -> dict[str, Any]:
        return {
            "id": run_id,
            "sweep_id": self._sweep.id,
            "strategy_id": strategy_id,
            "instrument_id": self._found[symbol].id,
            # The document's timeframe and the run's are one value, read from one place — which
            # is what makes PR-238's equality rule unreachable here.
            "timeframe": doc.timeframe,
            "date_from": self._request.date_from,
            "date_to": self._request.date_to,
            "initial_capital": self._request.initial_capital,
            "cost_model": self._costs[symbol],
            "status": BacktestStatus.QUEUED,
            "engine_version": ENGINE_VERSION,
        }

    def _links(self, run_id: uuid.UUID, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        """The run's downloads, when its pair is being collected.

        ⚠️ **One collection per window of each pair, linked to every run over it** — never one
        per run, which would download the same window once per point. Written the first time
        the pair is met; the worker needs no change: each run asks whether *its* collections
        have landed.
        """
        pair = (symbol, timeframe)
        plan = self._collectable.get(pair)
        if plan is None:
            return []
        downloads = self._downloads.get(pair)
        if downloads is None:
            downloads = [
                create_collection(
                    self._session,
                    symbol=symbol,
                    timeframe=timeframe,
                    date_from=window.date_from,
                    date_to=window.date_to,
                    # The class the catalogue already decided, as the basket does it: the
                    # broker's tree path must not refuse a collection the launch itself asked for.
                    asset_class=self._found[symbol].asset_class,
                    years_total=len(year_slices(window.date_from, window.date_to)),
                )
                for window in plan.windows
            ]
            self._session.flush()
            self._downloads[pair] = downloads
            self.collections += downloads
        return [{"backtest_id": run_id, "collection_id": collection.id} for collection in downloads]


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
    work, and would ship every run of every sweep on the page to say "480 of 500 done". The points
    live in `sweep_points` and are not read at all. Held by
    `test_listing_sweeps_costs_the_same_however_many_there_are`.
    """
    total = session.scalar(select(func.count()).select_from(Sweep)) or 0
    sweeps = session.scalars(
        select(Sweep)
        # `created_at` is the launch transaction's start, so it only ties between sweeps launched
        # in the same instant; the id breaks that tie so a page boundary cannot move between reads.
        .order_by(Sweep.created_at.desc(), Sweep.id)
        .limit(limit)
        .offset(offset)
    ).all()

    counts: dict[uuid.UUID, dict[BacktestStatus, int]] = {
        sweep.id: dict.fromkeys(BacktestStatus, 0) for sweep in sweeps
    }
    # A combination has no runs of its own: its members' are counted for it (26/09).
    owner: dict[uuid.UUID, list[uuid.UUID]] = {}
    for sweep in sweeps:
        for member in scope_of(sweep):
            owner.setdefault(member, []).append(sweep.id)
    for member_id, run_status, how_many in session.execute(
        select(Backtest.sweep_id, Backtest.status, func.count())
        .where(Backtest.sweep_id.in_(list(owner)))
        .group_by(Backtest.sweep_id, Backtest.status)
    ).all():
        if member_id is None:  # pragma: no cover — the filter selects by sweep
            continue
        for sweep_id in owner[member_id]:
            counts[sweep_id][run_status] += how_many

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
                combines=None if sweep.combines is None else len(sweep.combines),
                template_id=sweep.template_id,
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

    ⚠️ **The points are read here, unlike the history list.** They are the only record of which
    entry a run belongs to — the strategy's name is a caption, and recovering the entry from it
    breaks the day one entry's name is a prefix of another's. The equity curve stays deferred.
    """
    if launched_from is not None and launched_to is not None and launched_to <= launched_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="launched_to must come after launched_from",
        )

    # A combination has no runs of its own and would name its members' twice (26/09).
    window: list[ColumnElement[bool]] = [Sweep.combines.is_(None)]
    if launched_from is not None:
        window.append(Sweep.created_at >= launched_from)
    if launched_to is not None:
        window.append(Sweep.created_at < launched_to)
    key = _dashboard_key(session, window, launched_from, launched_to)
    kept = _DASHBOARD.get(key)
    if kept is not None:
        return kept

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
    entry_of: dict[uuid.UUID, dict[str, str]] = {sweep.id: {} for sweep in sweeps}
    for sweep_id, strategy_id, entry_id in session.execute(
        select(PointRow.sweep_id, PointRow.strategy_id, PointRow.entry_id).where(
            PointRow.sweep_id.in_(list(entry_of)), PointRow.same_as.is_(None)
        )
    ):
        entry_of[sweep_id][str(strategy_id)] = entry_id

    # ⚠️ **Columns, not rows** (25/09). Every run of every sweep read as an ORM object with its
    # metrics loaded beside it took 10.3 s on 80 thousand runs; the dashboard reads eleven of their
    # fields and six of the metrics', and a tuple per run is what it needs.
    rows = session.execute(
        select(
            Backtest.sweep_id,
            Backtest.strategy_id,
            Backtest.instrument_id,
            Backtest.timeframe,
            Backtest.status,
            Backtest.date_from,
            Backtest.date_to,
            Backtest.initial_capital,
            # As text, from Postgres: the key two copies of a measurement are compared by, and
            # `jsonb`'s own text is canonical — equal values print alike — so parsing it into a
            # dict only to print it again was 2 s of 5 on the same read.
            cast(Backtest.cost_model, Text).label("cost_model"),
            Backtest.engine_version,
            Instrument.symbol,
            BacktestMetrics.backtest_id,
            BacktestMetrics.net_profit,
            BacktestMetrics.total_trades,
            BacktestMetrics.win_rate,
            BacktestMetrics.profit_factor,
            BacktestMetrics.expectancy,
            BacktestMetrics.max_drawdown_pct,
        )
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .outerjoin(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .where(Backtest.sweep_id.in_(list(entry_of)))
        # Launch order, so which copy of a repeated measurement is kept does not change between
        # two reads of the same data.
        .order_by(Backtest.created_at, Backtest.id)
    ).all()

    runs: list[dashboard.DashboardRun] = []
    for row in rows:
        if row.sweep_id is None:  # pragma: no cover — the filter above selects by sweep
            continue
        entry_id = entry_of[row.sweep_id].get(str(row.strategy_id), "")
        runs.append(
            dashboard.DashboardRun(
                sweep_id=str(row.sweep_id),
                entry_id=entry_id,
                entry_name=names.get(entry_id),
                symbol=row.symbol,
                timeframe=row.timeframe,
                status=row.status,
                initial_capital=row.initial_capital,
                measurement=(
                    row.strategy_id,
                    row.instrument_id,
                    row.timeframe,
                    row.date_from,
                    row.date_to,
                    row.initial_capital,
                    row.cost_model,
                    row.engine_version,
                ),
                # A run has a result once its metrics row exists — `done` alone is not enough
                # (`aggregate_points` says why), and the outer join says which is which.
                result=None
                if row.backtest_id is None
                else dashboard.RunResult(
                    net_profit=row.net_profit,
                    total_trades=row.total_trades,
                    win_rate=row.win_rate,
                    profit_factor=row.profit_factor,
                    expectancy=row.expectancy,
                    max_drawdown_pct=row.max_drawdown_pct,
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
    out = SweepDashboardOut(
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
    if len(_DASHBOARD) >= _DASHBOARD_KEPT:
        _DASHBOARD.clear()
    _DASHBOARD[key] = out
    return out


_DASHBOARD: dict[tuple[Any, ...], SweepDashboardOut] = {}
"""The dashboards last computed, by what they were computed from (`_dashboard_key`) — 26/09.

⚠️ **A cache the key keeps honest.** Nothing is invalidated by hand: a run that lands, fails, is
retried or deleted, a sweep launched or removed, an entry renamed — each changes the key, and the
next read computes again. Measured 25/09: ~5 s to compute over 130 thousand runs, spread across
the read, the loop and the medians with no one part worth a rewrite; the key costs one query.
One process serves the API, so one dictionary is the whole cache; it is lost on a restart, and
the first read after costs what it always did."""

_DASHBOARD_KEPT = 16
"""Windows remembered at once — the screen asks for a handful; past this it starts over."""


def _dashboard_key(
    session: Session,
    window: list[ColumnElement[bool]],
    launched_from: dt.datetime | None,
    launched_to: dt.datetime | None,
) -> tuple[Any, ...]:
    """What the dashboard over `window` depends on, read in one aggregate and one small query:
    how many sweeps, runs, results, runs still open, the latest to end and to be launched, and
    the names of the entries it would print."""
    measured = session.execute(
        select(
            func.count(func.distinct(Sweep.id)),
            func.count(Backtest.id),
            func.count(BacktestMetrics.backtest_id),
            func.count(Backtest.id).filter(Backtest.status == BacktestStatus.RUNNING),
            func.count(Backtest.id).filter(Backtest.status == BacktestStatus.FAILED),
            func.max(Backtest.finished_at),
            func.max(Sweep.created_at),
        )
        .select_from(Sweep)
        .outerjoin(Backtest, Backtest.sweep_id == Sweep.id)
        .outerjoin(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .where(*window)
    ).one()
    asked = {
        one for (ids,) in session.execute(select(Sweep.entry_ids).where(*window)) for one in ids
    }
    names = sorted(
        (str(entry_id), name)
        for entry_id, name in session.execute(
            select(CatalogEntry.id, CatalogEntry.name).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in asked])
            )
        )
    )
    return (launched_from, launched_to, tuple(measured), tuple(names))


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
    holdout, runs, uncovered = launch_holdout(session, sweep_id, request)
    # After the commit, and with the run's own id as the job id — the sweep launch's reasons.
    for one in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(one.id), _job_id=str(one.id))
    return CreatedSweep(id=holdout.id, runs=len(runs), skipped=uncovered)


def launch_window(
    session: Session, parent: Sweep, date_from: dt.datetime, date_to: dt.datetime
) -> tuple[Sweep, list[Backtest]]:
    """`parent` again over another window — every run it made, the same strategy, market, chart,
    capital and costs — kept and committed; the caller queues the runs (a walk-forward's
    training, 25/09).

    ⚠️ **Its runs, not its grid again.** The parent's runs are what its launch made of the grid
    after refusals and de-duplication (`same_as`), so copying them reproduces the sweep exactly —
    expanding the grid anew would re-decide both, possibly differently. A (market, chart) with no
    candles in the new window is left out and recorded in `skipped`, as a launch does.
    """
    parents = session.execute(
        select(Backtest, Instrument.symbol)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id.in_(scope_of(parent)))
        .order_by(Backtest.created_at, Backtest.id)
    ).all()
    uncovered = uncovered_markets(
        session, list(parent.symbols), list(parent.timeframes), date_from, date_to
    )
    empty = {(market.symbol, market.timeframe) for market in uncovered}
    sweep = Sweep(
        entry_ids=list(parent.entry_ids),
        symbols=list(parent.symbols),
        timeframes=list(parent.timeframes),
        date_from=date_from,
        date_to=date_to,
        initial_capital=parent.initial_capital,
        skipped=[market.model_dump(mode="json") for market in uncovered],
    )
    session.add(sweep)
    session.flush()
    _copy_points(session, parent, sweep)
    runs = [
        Backtest(
            sweep=sweep,
            strategy_id=run.strategy_id,
            instrument_id=run.instrument_id,
            timeframe=run.timeframe,
            date_from=date_from,
            date_to=date_to,
            initial_capital=run.initial_capital,
            cost_model=dict(run.cost_model),
            status=BacktestStatus.QUEUED,
            engine_version=ENGINE_VERSION,
        )
        for run, symbol in parents
        if (symbol, run.timeframe) not in empty
    ]
    session.add_all(runs)
    session.commit()
    return sweep, runs


@router.post(
    "/sweeps/combine",
    response_model=CreatedSweep,
    status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
def combine_sweeps(request: CombineSweeps, session: SessionDep) -> CreatedSweep:
    """Keep a sweep that reads several finished sweeps as one — a template's markets, run one at a
    time over days, brought together for the second phase (26/09). Nothing runs.

    ⚠️ **Only sweeps that asked the same question.** The same entries, charts, window and capital
    — what a template fixes — or the medians, the choice of the best and the comparison would
    mix answers to different questions. Refused, with the reason, for a reserved-window test or
    another combination (its runs are chosen or borrowed), and for a sweep still running (the
    second phase reads finished work).
    """
    ids = list(dict.fromkeys(request.sweep_ids))
    if len(ids) < 2:  # noqa: PLR2004 — a combination of one is that sweep
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="combine at least two different sweeps",
        )
    found = {one.id: one for one in session.scalars(select(Sweep).where(Sweep.id.in_(ids)))}
    missing = [str(one) for one in ids if one not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"no such sweep: {', '.join(missing)}"
        )
    members = [found[one] for one in ids]
    for one in members:
        if one.holdout_rule is not None or one.combines is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{one.id} is a reserved-window test or a combination; combine the sweeps "
                "that ran",
            )

    def question(sweep: Sweep) -> tuple[Any, ...]:
        return (
            tuple(sorted(sweep.entry_ids)),
            tuple(sorted(sweep.timeframes)),
            sweep.date_from,
            sweep.date_to,
            sweep.initial_capital,
        )

    if len({question(one) for one in members}) != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="these sweeps asked different questions — entries, charts, window or capital "
            "differ; combine sweeps of one template",
        )
    counted = {one.id: _count_runs(session, one) for one in members}
    still = [str(key) for key, one in counted.items() if one.queued + one.running > 0]
    if still:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"still running: {', '.join(still)} — combine them once they have finished",
        )

    first = members[0]
    symbols = list(dict.fromkeys(symbol for one in members for symbol in one.symbols))
    templates = {one.template_id for one in members}
    combined = Sweep(
        entry_ids=list(first.entry_ids),
        symbols=symbols,
        timeframes=list(first.timeframes),
        date_from=first.date_from,
        date_to=first.date_to,
        initial_capital=first.initial_capital,
        skipped=[market for one in members for market in one.skipped],
        template_id=templates.pop() if len(templates) == 1 else None,
        combines=[str(one.id) for one in members],
    )
    session.add(combined)
    session.flush()
    # Each member's points, a point shared by two members (the same strategy, label and answer)
    # kept once, in the members' order.
    member = case({one.id: index for index, one in enumerate(members)}, value=PointRow.sweep_id)
    ranked = (
        select(
            PointRow.strategy_id,
            PointRow.entry_id,
            PointRow.label,
            PointRow.coordinates,
            PointRow.same_as,
            member.label("member"),
            PointRow.position,
            func.row_number()
            .over(
                partition_by=(PointRow.strategy_id, PointRow.label, PointRow.same_as),
                order_by=(member, PointRow.position),
            )
            .label("copy"),
        )
        .where(PointRow.sweep_id.in_([one.id for one in members]))
        .subquery()
    )
    session.execute(
        insert(PointRow).from_select(
            ["sweep_id", "position", "strategy_id", "entry_id", "label", "coordinates", "same_as"],
            select(
                literal(combined.id),
                func.row_number().over(order_by=(ranked.c.member, ranked.c.position)) - 1,
                ranked.c.strategy_id,
                ranked.c.entry_id,
                ranked.c.label,
                ranked.c.coordinates,
                ranked.c.same_as,
            ).where(ranked.c.copy == 1),
        )
    )
    session.commit()
    counts = _count_runs(session, combined)
    return CreatedSweep(
        id=combined.id,
        runs=counts.total,
        skipped=[UncoveredMarket.model_validate(one) for one in combined.skipped],
    )


def launch_holdout(
    session: Session, sweep_id: uuid.UUID, request: CreateHoldout
) -> tuple[Sweep, list[Backtest], list[UncoveredMarket]]:
    """Choose, keep and commit a reserved-window test of `sweep_id`; the caller queues its runs.

    Shared by the endpoint above and a sweep's walk-forward (`sweep_walkforward_job`), which
    tests each fold's training sweep the same way. Refusals are `HTTPException`s with the
    sentence a person reads, which the walk-forward records on the fold.
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

    own, _followers = _points_of(session, parent)
    runs_of: dict[int, tuple[Backtest, str]] = {}
    candidates: list[Candidate] = []
    for order, (run, symbol) in enumerate(_runs_of(session, parent, done_only=True)):
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
    session.flush()
    # The parent's own point records, copied: the same strategies at the same coordinates — the
    # one thing this sweep changes is the window.
    _copy_points(session, parent, holdout, strategy_ids=tested)
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
    return holdout, runs, uncovered


def _runs_of(
    session: Session, sweep: Sweep, *, done_only: bool = False
) -> list[tuple[Backtest, str]]:
    """A sweep's runs with their market, in launch order, metrics loaded without the heavy
    columns — the curve and the ladder, which neither a choice nor a comparison reads."""
    query = (
        select(Backtest, Instrument.symbol)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id.in_(scope_of(sweep)))
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
            for run, symbol in _runs_of(session, parent)
        }
    )
    own, _followers = _points_of(session, holdout)
    names = {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in holdout.entry_ids])
            )
        )
    }

    rows: list[HoldoutRow] = []
    for run, symbol in _runs_of(session, holdout):
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
    runs = _runs_of(session, test)
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

    own, _followers = _points_of(session, test)
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
    own, _followers = _points_of(session, test)
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

    ⚠️ **The coordinates come from `sweep_points`, never from the strategy's name.** The name
    carries the label (`9.1 sem filtro [M15 · period=5]`) because that is what a run log row
    shows — but it is a caption, and recovering axis values by splitting one works until an
    entry's name is a prefix of another's or a value contains the separator. The first draft of
    this endpoint did exactly that; the column exists because of it.
    """
    sweep = session.get(Sweep, sweep_id)
    if sweep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="sweep not found")
    counts, summaries = _summary(session, sweep)

    out: list[SweepRunOut] = []
    if runs == "all":
        _sweep, entries, coordinates, followers, rows = _read_sweep(session, sweep_id)
        for run, strategy, instrument in rows:
            point = coordinates.get(str(run.strategy_id), {})
            out.append(
                _run_out((run, strategy, instrument), point, entries=entries, followers=followers)
            )

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
        combines=None
        if sweep.combines is None
        else [uuid.UUID(str(one)) for one in sweep.combines],
        template_id=sweep.template_id,
        counts=counts,
        entries=summaries,
        runs=out,
        skipped=[UncoveredMarket.model_validate(one) for one in sweep.skipped],
        failed_collections=failed_collections(session, Backtest.sweep_id.in_(scope_of(sweep))),
    )


def _count_runs(session: Session, sweep: Sweep) -> SweepRunCounts:
    """The runs per status, counted by Postgres — 14 ms on 51 840 runs (25/09)."""
    counted = dict.fromkeys(BacktestStatus, 0)
    for state, many in session.execute(
        select(Backtest.status, func.count())
        .where(Backtest.sweep_id.in_(scope_of(sweep)))
        .group_by(Backtest.status)
    ):
        counted[state] = many
    return SweepRunCounts(
        total=sum(counted.values()),
        done=counted[BacktestStatus.DONE],
        running=counted[BacktestStatus.RUNNING],
        queued=counted[BacktestStatus.QUEUED],
        failed=counted[BacktestStatus.FAILED],
    )


def _summary(session: Session, sweep: Sweep) -> tuple[SweepRunCounts, list[SweepEntryOut]]:
    """The run counts and each entry's summary — kept once every run has ended (25/09).

    ⚠️ **Served from `sweeps.summary` only while the counts still match** the ones it was computed
    at, and only once nothing is queued or running. A run retried or deleted changes the counts,
    and the summary is computed again and kept again. Measured 25/09 on 51 840 runs: 6.5 s a poll
    computed, a single count query kept.

    ⚠️ **Computed from four columns, not from the runs whole.** Each run's status, strategy, market
    and the two stored results the summary reads — not the strategy's document nor any ORM row,
    which were most of those 6.5 s. The entry's name is read live: a shelf label can be renamed,
    and a kept summary must not freeze it.
    """
    counts = _count_runs(session, sweep)
    settled = counts.queued == 0 and counts.running == 0
    names = _entry_names(session, sweep)
    kept = sweep.summary
    if settled and kept is not None and kept.get("counts") == counts.model_dump(mode="json"):
        return counts, [
            SweepEntryOut.model_validate({**one, "entry_name": names.get(str(one["entry_id"]))})
            for one in kept["entries"]
        ]

    coordinates, _followers = _points_of(session, sweep)
    points: dict[str, list[tuple[BacktestStatus, Decimal | None, str]]] = {
        one: [] for one in sweep.entry_ids
    }
    ladders: dict[str, list[tuple[dict[str, Any] | None, str]]] = {
        one: [] for one in sweep.entry_ids
    }
    for state, strategy_id, symbol, net, targets in session.execute(
        select(
            Backtest.status,
            Backtest.strategy_id,
            Instrument.symbol,
            BacktestMetrics.net_profit,
            BacktestMetrics.targets,
        )
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .outerjoin(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .where(Backtest.sweep_id.in_(scope_of(sweep)))
    ):
        point = coordinates.get(str(strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        if entry_id not in points:
            continue
        # The symbol leads because the point label repeats once per market: `M15 · period=9`
        # over three symbols is three runs, and a best that named only the point would name all
        # three of them.
        label = f"{symbol} · {point.get('label', '')}"
        points[entry_id].append((state, net, label))
        ladders[entry_id].append((targets, label))

    summaries = [
        SweepEntryOut(
            entry_id=uuid.UUID(entry_id),
            entry_name=names.get(entry_id),
            aggregate=aggregate_scored(points[entry_id], sweep.initial_capital),
            targets=rungs_of(ladders[entry_id]),
        )
        for entry_id in sweep.entry_ids
    ]
    if settled:
        sweep.summary = {
            "counts": counts.model_dump(mode="json"),
            "entries": [one.model_dump(mode="json") for one in summaries],
        }
        session.commit()
    return counts, summaries


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
    # ⚠️ **The entry's strategies as a subquery, not a list read first** (26/09): a sweep can hold
    # hundreds of thousands of points, and this page is polled. Only the page's own points are
    # read, below.
    strategies = select(PointRow.strategy_id).where(
        PointRow.sweep_id == sweep.id, PointRow.same_as.is_(None)
    )
    if entry_id is not None:
        strategies = strategies.where(PointRow.entry_id == str(entry_id))
    where = (Backtest.sweep_id.in_(scope_of(sweep)), Backtest.strategy_id.in_(strategies))
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
    own, followers = _points_of(
        session, sweep, strategy_ids={run.strategy_id for run, _, _ in rows}
    )
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


def scope_of(sweep: Sweep) -> list[uuid.UUID]:
    """The sweeps whose runs are this sweep's: itself, or — for a combination (26/09) — the
    sweeps it reads together. Every read of a sweep's runs filters on this, never on the id."""
    if sweep.combines:
        return [uuid.UUID(str(one)) for one in sweep.combines]
    return [sweep.id]


def _points_of(
    session: Session, sweep: Sweep, *, strategy_ids: set[uuid.UUID] | None = None
) -> tuple[dict[str, _Point], dict[str, list[_Point]]]:
    """A sweep's points by strategy: the one each run is, and the ones it answers besides — of
    `strategy_ids` only, when given (a page of runs reads its own points, not the sweep's).

    ⚠️ **Split on `same_as`, never on order.** A point answered by another's run carries that
    run's strategy id (`_SweepWriter`), so a plain `{strategy_id: point}` would keep whichever
    came last — and a run would be shown at a follower's coordinates rather than its own.
    """
    own: dict[str, _Point] = {}
    followers: dict[str, list[_Point]] = {}
    query = (
        select(
            PointRow.strategy_id,
            PointRow.entry_id,
            PointRow.label,
            PointRow.coordinates,
            PointRow.same_as,
        )
        .where(PointRow.sweep_id == sweep.id)
        .order_by(PointRow.position)
    )
    if strategy_ids is not None:
        query = query.where(PointRow.strategy_id.in_(list(strategy_ids)))
    for strategy_id, entry_id, label, coordinates, same_as in session.execute(query):
        key = str(strategy_id)
        point: _Point = {
            "strategy_id": key,
            "entry_id": entry_id,
            "label": label,
            "values": coordinates,
        }
        if same_as is None:
            own[key] = point
        else:
            followers.setdefault(key, []).append({**point, "same_as": same_as})
    return own, followers


def _copy_points(
    session: Session, source: Sweep, target: Sweep, *, strategy_ids: set[str] | None = None
) -> None:
    """`source`'s points written again as `target`'s, in their order — in Postgres, never
    through Python: a sweep's points can number hundreds of thousands (26/09).

    With `strategy_ids`, only the points that are those strategies' own runs (a reserved-window
    test copies the points it chose, and none of the points they answered).
    """
    query = select(
        literal(target.id),
        func.row_number().over(order_by=PointRow.position) - 1,
        PointRow.strategy_id,
        PointRow.entry_id,
        PointRow.label,
        PointRow.coordinates,
        PointRow.same_as,
    ).where(PointRow.sweep_id == source.id)
    if strategy_ids is not None:
        query = query.where(
            PointRow.same_as.is_(None),
            PointRow.strategy_id.in_([uuid.UUID(one) for one in strategy_ids]),
        )
    session.execute(
        insert(PointRow).from_select(
            ["sweep_id", "position", "strategy_id", "entry_id", "label", "coordinates", "same_as"],
            query,
        )
    )


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
    coordinates, followers = _points_of(session, sweep)

    rows = session.execute(
        select(Backtest, Strategy, Instrument)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .where(Backtest.sweep_id.in_(scope_of(sweep)))
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
