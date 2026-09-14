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

⚠️ **The preview subtracts refusals; the launch refuses nothing quietly.** A combination that
cannot run is reported per entry before anything is written, and the sweep then enqueues exactly
what the preview promised. A sweep that silently dropped points would draw a map of a space it
never searched, and it would look exactly like a map of one it did.
"""

import datetime as dt
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.grid import GridPoint
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.routers.backtests import list_item
from tradeforge_api.routers.strategies import refusal_of
from tradeforge_api.routers.studies import strategies_for
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    CreatedSweep,
    CreateSweep,
    GridRefusal,
    PreviewSweepRequest,
    SweepEntryPreview,
    SweepOut,
    SweepPreview,
    SweepRunOut,
    UncoveredMarket,
)
from tradeforge_api.sweep import (
    SweepDocument,
    SweepError,
    documents_for,
    points_in,
    size_refusal,
)
from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    CatalogEntry,
    Dataset,
    Instrument,
    Strategy,
    Sweep,
)

router = APIRouter(tags=["sweeps"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


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


def _uncovered(
    session: SessionDep,
    symbols: list[str],
    timeframes: list[str],
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> list[UncoveredMarket]:
    """Which (symbol, timeframe) pairs hold no candles inside this window.

    ⚠️ **Asked of the index, not of the files.** `datasets` exists to answer "do I have EURUSD H1
    for 2021?" with a lookup rather than by opening Parquet (ADR-05), and that is the whole
    reason this check is cheap enough to run before a launch.

    ⚠️ **This exists because the first real sweep lost nine runs of twelve to it.** Each failure
    was honest — the worker said exactly which window the data covers — but it arrived after
    twelve jobs had been enqueued, which is the round trip a preview exists to remove. A pair
    that has never been collected and a pair collected for other years are reported apart: one
    is a backfill to run, the other is a window to move.
    """
    instruments = {
        instrument.symbol: instrument
        for instrument in session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols)))
    }
    coverage = {
        (row.instrument_id, row.timeframe): row
        for row in session.scalars(
            select(Dataset).where(
                Dataset.instrument_id.in_([one.id for one in instruments.values()]),
                Dataset.timeframe.in_(timeframes),
            )
        )
    }

    out: list[UncoveredMarket] = []
    for symbol in symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            continue  # an unknown symbol is a different refusal, and the caller makes it first
        for timeframe in timeframes:
            dataset = coverage.get((instrument.id, timeframe))
            if dataset is None:
                out.append(UncoveredMarket(symbol=symbol, timeframe=timeframe, covers=None))
            elif dataset.date_from >= date_to or dataset.date_to <= date_from:
                # No overlap at all. A *partial* overlap is deliberately allowed: a run over the
                # half of the window that exists is a real measurement, and refusing it would
                # make every sweep wait for the least-collected symbol on the list.
                out.append(
                    UncoveredMarket(
                        symbol=symbol,
                        timeframe=timeframe,
                        covers=(
                            f"{dataset.date_from.date().isoformat()} to "
                            f"{dataset.date_to.date().isoformat()}"
                        ),
                    )
                )
    return out


@router.post("/sweeps/preview", response_model=SweepPreview, responses={**_NOT_FOUND, **_BAD_BODY})
def preview_sweep(request: PreviewSweepRequest, session: SessionDep) -> SweepPreview:
    """What this sweep would enqueue, without enqueuing any of it.

    ⚠️ **The browser cannot answer this and must not try.** Whether a combination can run is the
    DSL's semantics, and those live in Python once — a screen that reimplemented them would be a
    second copy of the contract, wrong the day either changed.

    Reports rather than decides, like the study's preview and for the same reason: a person
    fixing one axis per round trip is the round trip this endpoint exists to remove. The cap is
    the one refusal it reports as an `error` instead, because a sweep over the cap has no runs to
    describe.
    """
    pairs = _entries(session, request.entry_ids)

    # ⚠️ **The same guard the launch has, and the preview needs it more.** Without it a
    # backwards window overlaps no dataset at all, so `_uncovered` reports every market and the
    # screen reads "no candles in this window; move the window or collect them first" — blaming
    # the data for a typo, and sending a person to run a backfill they do not need. Refused the
    # same way and in the same words the launch refuses it, so one request cannot get two
    # verdicts from the two endpoints.
    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    timeframes = list(request.timeframes)
    documents = _expand(pairs, timeframes)

    per_entry: list[SweepEntryPreview] = []
    runnable = 0
    for entry, _strategy in pairs:
        mine = [doc for doc in documents if doc.entry_id == str(entry.id)]
        refusals = [
            GridRefusal(label=doc.label, values=dict(doc.values), reason=reason)
            for doc in mine
            if (reason := refusal_of(dict(doc.document))) is not None
        ]
        runnable += len(mine) - len(refusals)
        per_entry.append(
            SweepEntryPreview(
                entry_id=entry.id,
                name=entry.name,
                points=points_in(entry.grid),
                refusals=refusals,
            )
        )

    uncovered = _uncovered(
        session, list(request.symbols), timeframes, request.date_from, request.date_to
    )

    runs = runnable * len(request.symbols)
    error = None
    if uncovered:
        error = (
            f"{len(uncovered)} of these markets have no candles in this window; "
            "move the window or collect them first"
        )
    else:
        # ⚠️ The cap and the empty sweep are asked of the module, not restated here — the
        # launch below asks the same function, so the two endpoints cannot answer this in
        # different words. Reported as an `error` rather than raised, which is the one thing
        # that *is* different: a preview that raised would have nothing to preview.
        error = size_refusal(runs)

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

    **Nothing is written until every combination has passed.** A sweep that half-exists is worse
    than one that was refused: the caller asked one question about a space, and four hundred runs
    plus an error answers a question nobody asked. The same doctrine as the study, one axis up.

    ⚠️ **Refused combinations are dropped, and the preview already said which.** That is not the
    silent trimming the caps refuse — a point that cannot run is not part of the space, and
    enqueuing it to fail would spend a worker to re-learn what `assert_executable` knew before
    anything started. What must never be dropped silently is a point that *could* have run, which
    is why the cap refuses the whole request instead.
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

    # ⚠️ Refused, not dropped, and named one by one. Enqueuing a run that cannot read a single
    # candle spends a worker to re-learn what the `datasets` index already knows — which is what
    # the first real sweep did, nine times out of twelve.
    uncovered = _uncovered(
        session, list(request.symbols), timeframes, request.date_from, request.date_to
    )
    if uncovered:
        named = ", ".join(f"{one.symbol} {one.timeframe}" for one in uncovered)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no candles in this window for: {named}",
        )

    documents = [
        doc for doc in _expand(pairs, timeframes) if refusal_of(dict(doc.document)) is None
    ]
    total = len(documents) * len(request.symbols)
    # The same question the preview asked, of the same function, so the words a person read on
    # the screen are the words they get back from the button.
    refusal = size_refusal(total)
    if refusal is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=refusal)

    sweep = Sweep(
        entry_ids=[str(one) for one in request.entry_ids],
        symbols=list(request.symbols),
        timeframes=timeframes,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
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
        for symbol in request.symbols:
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
    for run in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(run.id), _job_id=str(run.id))

    return CreatedSweep(id=sweep.id, runs=len(runs))


@router.get("/sweeps/{sweep_id}", response_model=SweepOut, responses=_NOT_FOUND)
def get_sweep(sweep_id: uuid.UUID, session: SessionDep) -> SweepOut:
    """A sweep read back: the question that was asked, and every run it became.

    ⚠️ **The coordinates come from `sweeps.points`, never from the strategy's name.** The name
    carries the label (`9.1 sem filtro [M15 · period=5]`) because that is what a run log row
    shows — but it is a caption, and recovering axis values by splitting one works until an
    entry's name is a prefix of another's or a value contains the separator. The first draft of
    this endpoint did exactly that; the column exists because of it.
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
    ).all()

    out: list[SweepRunOut] = []
    for run, strategy, instrument in rows:
        point = coordinates.get(str(run.strategy_id), {})
        entry_id = str(point.get("entry_id", ""))
        entry = entries.get(entry_id)
        out.append(
            SweepRunOut(
                entry_id=uuid.UUID(entry_id) if entry_id else uuid.UUID(int=0),
                # ⚠️ The shelf label if the entry is still there, and the document's own name if
                # it is not. Removing an entry is allowed by design and must not blank a finished
                # sweep — what it costs is the label, never the measurement.
                entry_name=entry.name if entry is not None else strategy.name,
                label=str(point.get("label", "")),
                values=dict(point.get("values", {})),
                run=list_item(run, instrument.symbol, strategy.name, strategy.version),
            )
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
        runs=out,
    )
