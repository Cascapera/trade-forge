"""`/studies` — one strategy over a grid of its own parameters, to see the shape of the result.

The sibling of `/baskets`, and the pair is the point. A basket varies the **market** and holds
the parameters still: *does this method survive outside the instrument it was built on?* A study
varies the **parameters** and holds the market still: *is this result a property of the method,
or of the corner I happened to pick?* Both exist because a single number from a single run
cannot answer either.

`POST` does what `POST /backtests` does, N times, in one transaction, and runs nothing itself.
Each combination becomes its own stored `Strategy` and its own queued `Backtest` — so every run
still answers "what did I execute?" by pointing at one immutable document, exactly like every
other run in the system.

⚠️ **The read side deliberately refuses to headline the winner.** A grid always has a best
point; a grid of pure noise has a best point. See `StudyAggregate`.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.grid import (
    GridError,
    GridPoint,
    coordinates,
    expand,
    label_for,
    named,
    read_point,
)
from tradeforge_api.queue import RUN_BACKTEST
from tradeforge_api.routers.backtests import list_item
from tradeforge_api.routers.strategies import validate_document
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.schemas import (
    CreatedStudy,
    CreateStudyRequest,
    StudyAggregate,
    StudyOut,
    StudyPointOut,
)
from tradeforge_collector import step
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    Study,
)

router = APIRouter(tags=["studies"])

_Responses = dict[int | str, dict[str, Any]]
_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "not found"}}
_BAD_BODY: _Responses = {status.HTTP_400_BAD_REQUEST: {"description": "malformed request body"}}


def points_for(base: Strategy, grid: Mapping[str, Sequence[Any]]) -> list[GridPoint]:
    """Expand the grid, or refuse — and validate every document before any of them is written.

    Public because `/walkforwards` runs the same grid over each of its folds, and it has to
    produce *identical* documents to the ones the study ran: the whole comparison is between a
    heatmap and a blind choice over the same parameter space. A second expansion written next
    door would be the same code until the day one of them was fixed.

    Two refusals, and the order matters. `expand` rejects a grid that cannot be applied to
    *this* document (an unreachable path, an empty axis, a product over the cap). Then each
    resulting document is put through the same validator `POST /strategies` uses, because a
    substituted value can be individually legal and still produce a strategy that cannot run —
    a period of zero, a risk-reward of minus one.

    **Nothing is written until both have passed for every point.** A study that half-exists is
    worse than one that was refused: the caller asked one question about a space, and sixty
    runs plus an error answers a question nobody asked.
    """
    try:
        points = expand(base.definition, grid)
    except GridError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    for point in points:
        # ⚠️ The name is written **into the document**, not onto the row. `strategies.name` is a
        # generated column (`definition ->> 'name'`), so the document is the only place a name
        # can be set — which is the DSL staying the single source of truth all the way into the
        # database. Done before validating, since the name is a field the DSL checks.
        point.document["name"] = named(base.name, point)
        # Raises the same 422 the strategy endpoint raises, carrying the same error body — so a
        # client already able to explain why a strategy was rejected can explain this one too.
        validate_document(point.document)
    return points


def strategies_for(session: SessionDep, points: list[GridPoint]) -> list[Strategy]:
    """One stored strategy per point, reusing an identical one where it already exists.

    Public for `/walkforwards`, where the reuse stops being a nicety and becomes the mechanism:
    a point's document carries no symbol and no dates, so `period=9` is byte-identical in every
    fold, and six folds over fifty points write fifty strategies rather than three hundred.

    ⚠️ **Reuse is not an optimisation here; without it the second study fails outright.** A
    point's document contains no symbol and no dates — those live on the run — so launching the
    same grid over a different market, or a different period, produces byte-identical
    documents. Written blindly at version 1 they would collide on the unique `(name, version)`
    the moment anyone ran a grid twice, and the failure would arrive as a 500 from an integrity
    error rather than as anything a caller could act on.

    Reused only when the document **matches**, never on the name alone. A name is a label and
    could have been used by hand for something else; running that instead would silently
    execute a different strategy under a caption saying otherwise. When the name is taken by a
    different document the point becomes the next version of it, which is exactly what the
    strategies endpoint means by an edit.

    ⚠️ **A fresh point carries no parent, and a successor must.** `strategies` enforces
    `(version = 1) = (parent_version_id IS NULL)` — a lineage starts at version 1 with nothing
    behind it, and every later version has to name what it succeeds. A point's name is normally
    a name nothing has used, so it *is* a new lineage; the link back to the strategy the grid
    was built from is not lost, it lives on `studies.strategy_id`, the row whose whole job is
    to say these hundred came from that one. Recording it twice would be two answers to one
    question. Only when the name is already taken by a *different* document does a point become
    version N+1 — and then it must point at the version it follows, or the same constraint
    refuses it.
    """
    names = {str(point.document["name"]) for point in points}
    existing = list(session.scalars(select(Strategy).where(Strategy.name.in_(names))))

    identical = {(row.name, _key(row.definition)): row for row in existing}
    # The tip of each lineage: the version a successor would follow, and the row it points at.
    tip: dict[str, Strategy] = {}
    for row in existing:
        if row.name not in tip or row.version > tip[row.name].version:
            tip[row.name] = row

    built: list[Strategy] = []
    for point in points:
        name = str(point.document["name"])
        found = identical.get((name, _key(point.document)))
        if found is not None:
            built.append(found)
            continue
        # No bookkeeping for a second new row under the same name, because there cannot be
        # one: every point of a grid is a distinct combination, and a name is built from all of
        # its values, so within one call the names are distinct by construction.
        parent = tip.get(name)
        built.append(
            Strategy(
                definition=point.document,
                version=1 if parent is None else parent.version + 1,
                parent_version_id=None if parent is None else parent.id,
            )
        )
    return built


def _key(document: dict[str, Any]) -> str:
    """A document as a comparable string: sorted keys, so ordering is not part of the identity.

    Two documents that differ only in the order their keys were written are the same strategy,
    and JSONB does not preserve that order anyway — comparing the raw dicts would work by
    accident today and stop working the day one of them makes a round trip through Postgres.
    """
    return json.dumps(document, sort_keys=True, default=str)


@router.post(
    "/studies",
    response_model=CreatedStudy,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_NOT_FOUND, **_BAD_BODY},
)
async def create_study(
    request: CreateStudyRequest, session: SessionDep, queue: QueueDep
) -> CreatedStudy:
    """Validate once, write the study, its strategies and its runs in one transaction, enqueue.

    The strategies are written here rather than referenced because each point *is* a different
    strategy. Each is its own lineage at version 1, named for its own values — `MME9
    [period=9]` — rather than versions 1..N of the base one: both keep a run pointing at the
    exact document it executed, but only one produces a run log a human can read, and the run
    log is where a study's result actually gets looked at.
    """
    base = session.get(Strategy, request.strategy_id)
    if base is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="strategy not found")

    try:
        step(request.timeframe)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    if request.date_to < request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )

    instrument = session.scalar(select(Instrument).where(Instrument.symbol == request.symbol))
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown symbol: {request.symbol}",
        )

    points = points_for(base, request.grid)

    study = Study(
        strategy_id=base.id,
        instrument_id=instrument.id,
        timeframe=request.timeframe,
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
        grid=dict(request.grid),
    )
    session.add(study)

    # ⚠️ Each point is its own lineage, never a version of the base one. Versioning into the
    # base would march its number forward by the size of the grid, leaving the next honest edit
    # of the strategy at v101 — and a run log row reading `MME9 v37` says nothing about which
    # combination it was. The ancestry lives on the study, which is what `studies.strategy_id`
    # is for.
    strategies = strategies_for(session, points)
    session.add_all(strategies)
    # Flushed, not committed: `Backtest` reaches its strategy by id and there is no relationship
    # to defer the resolution — the ids have to exist before the runs are built. Still one
    # transaction, so a failure anywhere below still leaves nothing behind.
    session.flush()

    runs = [
        Backtest(
            study=study,
            strategy_id=strategy.id,
            instrument_id=instrument.id,
            timeframe=request.timeframe,
            date_from=request.date_from,
            date_to=request.date_to,
            initial_capital=request.initial_capital,
            cost_model=request.cost_model,
            status=BacktestStatus.QUEUED,
            engine_version=ENGINE_VERSION,
        )
        for strategy in strategies
    ]
    session.add_all(runs)
    try:
        session.commit()
    except IntegrityError as exc:
        # ⚠️ Two identical grids launched at the same moment. Both read `existing` before either
        # wrote, so both build the same `(name, version)` and the loser blocks on the unique
        # index until the winner commits — then raises. Measured, not theorised: the window runs
        # from the SELECT to the COMMIT, which is about half a second for five hundred points.
        #
        # The rollback is total, so nothing is left half-written; what was missing was a reply a
        # caller can act on. 409 is the same answer `strategies._persist` gives to the same
        # collision one layer down, which is why this is the shape rather than a retry: the
        # honest thing to tell a client is that the strategies it needs were created a moment
        # ago by someone else, and asking again will find and reuse them.
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="another study created these strategies at the same time; try again",
        ) from exc

    # ⚠️ No `session.refresh` loop here, and its absence is deliberate. The session is built with
    # `expire_on_commit=False` and the ids are UUIDs generated in Python, so every field this
    # response reads — `id`, `strategy_id`, `status` — is already on the object. Refreshing would
    # be one SELECT per point: five hundred sequential round trips, on the event loop, buying
    # nothing.

    # After the commit, exactly as the single-run endpoint does it: a worker is fast enough to
    # claim a job before an uncommitted row is visible, and would then fail looking up a
    # backtest that does exist.
    #
    # ⚠️ `_job_id` is the run's own id, which makes the enqueue **idempotent**. This loop is the
    # one part of the request that is not inside the transaction: if it dies at point k, the
    # study and all N runs are committed while the rest sit `queued` for ever. With the job id
    # derived from the run, re-sending the same study's jobs is safe — arq drops a duplicate —
    # so recovery is a retry rather than an archaeology exercise.
    for run in runs:
        await queue.enqueue_job(RUN_BACKTEST, str(run.id), _job_id=str(run.id))

    return CreatedStudy(
        id=study.id,
        points=[
            StudyPointOut(
                backtest_id=run.id,
                strategy_id=run.strategy_id,
                label=point.label,
                values=dict(point.values),
                status=run.status,
            )
            for point, run in zip(points, runs, strict=True)
        ],
    )


def _aggregate(runs: list[tuple[Backtest, str]], initial_capital: Decimal) -> StudyAggregate:
    """Dispersion across the finished points. Never the maximum alone — see `StudyAggregate`.

    A run counts as finished only when it has metrics. `status == done` is not enough on its
    own, and the difference is not pedantry: a run is marked done an instant before its metrics
    row is written, and reading `net_profit` off that gap would be an exception raised by a
    reporting endpoint.

    The **median**, not the mean. On a grid the mean is worse than uninformative — one
    spectacular corner drags it upward, which is precisely the corner a reader most needs to
    see reported as an outlier rather than baked into the summary.
    """
    finished = [(run, label) for run, label in runs if run.metrics is not None]
    failed = sum(1 for run, _ in runs if run.status == BacktestStatus.FAILED)

    # Fractions of the capital every point starts with — fixed by the study, so these are
    # comparable by construction rather than by the reader remembering to check.
    returns = sorted(
        (run.metrics.net_profit / initial_capital, label)  # type: ignore[union-attr]
        for run, label in finished
    )

    if not returns:
        return StudyAggregate(
            points_total=len(runs),
            points_finished=0,
            points_failed=failed,
            points_profitable=0,
            best_label=None,
            best_return=None,
            worst_label=None,
            worst_return=None,
            median_return=None,
        )

    middle, odd = divmod(len(returns), 2)
    median = returns[middle][0] if odd else (returns[middle - 1][0] + returns[middle][0]) / 2

    return StudyAggregate(
        points_total=len(runs),
        points_finished=len(returns),
        points_failed=failed,
        points_profitable=sum(1 for value, _ in returns if value > 0),
        worst_return=returns[0][0],
        worst_label=returns[0][1],
        best_return=returns[-1][0],
        best_label=returns[-1][1],
        median_return=median,
    )


@router.get("/studies/{study_id}", response_model=StudyOut, responses=_NOT_FOUND)
def get_study(study_id: uuid.UUID, session: SessionDep) -> StudyOut:
    """The study, every point in it, and how far apart those points landed.

    The equity curve is deferred for the same reason `/backtests` and `/baskets` defer it: a
    large JSONB column no field of this response can reach, and a grid has a hundred of them.
    """
    study = session.get(Study, study_id)
    if study is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="study not found")

    base = session.get(Strategy, study.strategy_id)
    instrument = session.get(Instrument, study.instrument_id)
    if base is None or instrument is None:  # pragma: no cover — both FKs are RESTRICT
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="study references nothing"
        )

    # Joined to `strategies` because each point has its own, and that document is what says
    # which point a row is.
    #
    # ⚠️ **Not ordered in SQL, and the obvious ordering is a trap.** `created_at` defaults to
    # `now()`, which in Postgres is the *transaction's* start — so every run of one study
    # carries the identical timestamp, the tiebreaker falls to a random UUID, and the rows come
    # back shuffled. Measured, not assumed: a six-point grid came back 20, 20, 9, 5, 9, 5.
    rows = session.execute(
        select(Backtest, Strategy)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .where(Backtest.study_id == study.id)
        .options(selectinload(Backtest.metrics).defer(BacktestMetrics.equity_curve))
    ).all()

    grid: dict[str, list[Any]] = dict(study.grid)
    # Each run's coordinates, read back out of the document that actually ran. Not parsed from
    # the strategy's name: a name is a caption, and splitting one on commas and equals signs
    # works until a value contains either.
    # ⚠️ `read_point` rather than `value_at` directly: `coordinates` tolerates a value the grid
    # no longer lists, and `value_at`, which runs first, would have raised for the same class of
    # hand-edited row. Two guards over one hazard have to agree, so both defend. Shared with the
    # walk-forward read, which names the point a fold chose by exactly this rule.
    placed = [(run, strategy, read_point(strategy.definition, grid)) for run, strategy in rows]
    placed.sort(key=lambda row: coordinates(grid, row[2]))

    return StudyOut(
        id=study.id,
        strategy_id=study.strategy_id,
        strategy_name=base.name,
        symbol=instrument.symbol,
        timeframe=study.timeframe,
        date_from=study.date_from,
        date_to=study.date_to,
        initial_capital=study.initial_capital,
        created_at=study.created_at,
        grid=grid,
        # ⚠️ `label_for(values)`, not `strategy.name`. The stored name carries the base
        # strategy's name in front of the values — `MME9 [period=20]` — and serving that here
        # while `points[].label` serves `period=20` leaves a client unable to match the best
        # point to any point. It is the same defect `label_for` was extracted to close, reopened
        # one call site over, and neither suite saw it: the unit tests pass the label *into*
        # `_aggregate`, so they prove the function rather than its caller.
        aggregate=_aggregate(
            [(run, label_for(values)) for run, _, values in placed], study.initial_capital
        ),
        points=[
            StudyPointOut(
                backtest_id=run.id,
                strategy_id=strategy.id,
                label=label_for(values),
                values=values,
                status=run.status,
            )
            for run, strategy, values in placed
        ],
        runs=[
            list_item(run, instrument.symbol, strategy.name, strategy.version)
            for run, strategy, _ in placed
        ],
    )
