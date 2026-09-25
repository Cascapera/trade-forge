"""Sweep templates (26/09): a sweep without its markets, and a queue that runs one market at a time.

His ask: run some markets today and others tomorrow, then read them together. A template keeps
entries, charts, window and capital; each market queued becomes a sweep of its own
(`Sweep.template_id`), launched by the conductor (`template_queue_job`) when the one before has
ended.
"""

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tradeforge_api.deps import QueueDep, SessionDep
from tradeforge_api.queue import RUN_TEMPLATE_QUEUE
from tradeforge_api.schemas import (
    CreateSweepTemplate,
    QueueMarkets,
    SweepTemplateListItem,
    SweepTemplateOut,
    TemplateItemOut,
)
from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    CatalogEntry,
    Instrument,
    SweepTemplate,
    SweepTemplateItem,
    TemplateItemStatus,
)

router = APIRouter(tags=["sweeps"])

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "not found"}
}


def _template(session: SessionDep, template_id: uuid.UUID) -> SweepTemplate:
    template = session.get(SweepTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="template not found")
    return template


@router.post(
    "/sweep-templates", response_model=SweepTemplateOut, status_code=status.HTTP_201_CREATED
)
def create_template(request: CreateSweepTemplate, session: SessionDep) -> SweepTemplateOut:
    """Keep a template. Refused for an entry not on the shelf, a window that runs backwards, and
    a name already taken — templates are chosen by name on the screen."""
    if request.date_to <= request.date_from:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="date_to precedes date_from"
        )
    found = set(
        session.scalars(select(CatalogEntry.id).where(CatalogEntry.id.in_(request.entry_ids)))
    )
    missing = [str(one) for one in request.entry_ids if one not in found]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"not on the shelf: {', '.join(missing)}",
        )
    template = SweepTemplate(
        name=request.name,
        entry_ids=[str(one) for one in request.entry_ids],
        timeframes=list(request.timeframes),
        date_from=request.date_from,
        date_to=request.date_to,
        initial_capital=request.initial_capital,
        paused=False,
    )
    session.add(template)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a template is already named {request.name!r}",
        ) from exc
    return template_out(session, template)


@router.get("/sweep-templates", response_model=list[SweepTemplateListItem])
def list_templates(session: SessionDep) -> list[SweepTemplateListItem]:
    """Every template, newest first, with how many markets it has in each state."""
    out: list[SweepTemplateListItem] = []
    for template in session.scalars(
        select(SweepTemplate).order_by(SweepTemplate.created_at.desc())
    ):
        counts = dict.fromkeys(TemplateItemStatus, 0)
        for item in template.items:
            counts[item.status] += 1
        out.append(
            SweepTemplateListItem(
                id=template.id,
                name=template.name,
                timeframes=list(template.timeframes),
                date_from=template.date_from,
                date_to=template.date_to,
                paused=template.paused,
                waiting=counts[TemplateItemStatus.WAITING],
                launched=counts[TemplateItemStatus.LAUNCHED],
                failed=counts[TemplateItemStatus.FAILED],
                created_at=template.created_at,
            )
        )
    return out


@router.get("/sweep-templates/{template_id}", response_model=SweepTemplateOut, responses=_NOT_FOUND)
def get_template(template_id: uuid.UUID, session: SessionDep) -> SweepTemplateOut:
    return template_out(session, _template(session, template_id))


@router.post(
    "/sweep-templates/{template_id}/queue",
    response_model=SweepTemplateOut,
    responses=_NOT_FOUND,
)
async def queue_markets(
    template_id: uuid.UUID, request: QueueMarkets, session: SessionDep, queue: QueueDep
) -> SweepTemplateOut:
    """Add markets to the end of the queue, each with its costs, and wake the conductor.

    ⚠️ **A blank spread is the market's measured one, written now** — what the screen prefilled
    and he left as it was. Refused, with the markets named, for a symbol that has never been
    collected, and for one with no spread given and none measured: a sweep never charges a market
    nothing by omission.
    """
    template = _template(session, template_id)
    symbols = [one.symbol for one in request.markets]
    found = {
        one.symbol: one
        for one in session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols)))
    }
    problems: list[str] = []
    costs: list[dict[str, Any]] = []
    for market in request.markets:
        instrument = found.get(market.symbol)
        if instrument is None:
            problems.append(f"{market.symbol}: never collected")
            continue
        spread = market.spread_points
        if spread is None:
            if instrument.default_spread_points is None:
                problems.append(f"{market.symbol}: no spread measured — type one")
                continue
            spread = instrument.default_spread_points
        cost: dict[str, Any] = {
            "spread_points": str(Decimal(spread).normalize()),
            "commission_per_unit": str(market.commission_per_unit),
        }
        if market.swap_long_per_lot is not None or market.swap_short_per_lot is not None:
            cost["swap_long_per_lot"] = str(market.swap_long_per_lot or 0)
            cost["swap_short_per_lot"] = str(market.swap_short_per_lot or 0)
        costs.append(cost)
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="cannot queue: " + "; ".join(problems),
        )

    last = session.scalar(
        select(func.max(SweepTemplateItem.position)).where(
            SweepTemplateItem.template_id == template.id
        )
    )
    start = 0 if last is None else last + 1
    session.add_all(
        SweepTemplateItem(
            template_id=template.id,
            symbol=market.symbol,
            cost_model=cost,
            position=start + index,
            status=TemplateItemStatus.WAITING,
        )
        for index, (market, cost) in enumerate(zip(request.markets, costs, strict=True))
    )
    session.commit()
    await queue.enqueue_job(RUN_TEMPLATE_QUEUE, str(template.id))
    session.refresh(template)
    return template_out(session, template)


@router.post(
    "/sweep-templates/{template_id}/pause", response_model=SweepTemplateOut, responses=_NOT_FOUND
)
def pause_template(template_id: uuid.UUID, session: SessionDep) -> SweepTemplateOut:
    """Launch nothing new; the market already running goes on to its end."""
    template = _template(session, template_id)
    template.paused = True
    session.commit()
    return template_out(session, template)


@router.post(
    "/sweep-templates/{template_id}/resume", response_model=SweepTemplateOut, responses=_NOT_FOUND
)
async def resume_template(
    template_id: uuid.UUID, session: SessionDep, queue: QueueDep
) -> SweepTemplateOut:
    template = _template(session, template_id)
    template.paused = False
    session.commit()
    await queue.enqueue_job(RUN_TEMPLATE_QUEUE, str(template.id))
    return template_out(session, template)


@router.delete(
    "/sweep-templates/{template_id}/items/{item_id}",
    response_model=SweepTemplateOut,
    responses=_NOT_FOUND,
)
def remove_item(
    template_id: uuid.UUID, item_id: uuid.UUID, session: SessionDep
) -> SweepTemplateOut:
    """Take a market out of the queue. Only one still waiting: a launched market is a sweep, and
    a sweep is stopped by its own runs, not by the queue."""
    template = _template(session, template_id)
    item = session.get(SweepTemplateItem, item_id)
    if item is None or item.template_id != template.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")
    if item.status is not TemplateItemStatus.WAITING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{item.symbol} is {item.status.value}; only a waiting market can be removed",
        )
    item.status = TemplateItemStatus.REMOVED
    session.commit()
    return template_out(session, template)


def template_out(session: SessionDep, template: SweepTemplate) -> SweepTemplateOut:
    """The template with each market's place, costs and how far its sweep has come."""
    sweep_ids = [one.sweep_id for one in template.items if one.sweep_id is not None]
    counts: dict[uuid.UUID, dict[BacktestStatus, int]] = {}
    for sweep_id, state, many in session.execute(
        select(Backtest.sweep_id, Backtest.status, func.count())
        .where(Backtest.sweep_id.in_(sweep_ids))
        .group_by(Backtest.sweep_id, Backtest.status)
    ):
        if sweep_id is not None:
            counts.setdefault(sweep_id, {})[state] = many
    names = {
        str(entry.id): entry.name
        for entry in session.scalars(
            select(CatalogEntry).where(
                CatalogEntry.id.in_([uuid.UUID(one) for one in template.entry_ids])
            )
        )
    }
    items: list[TemplateItemOut] = []
    for item in template.items:
        of = counts.get(item.sweep_id, {}) if item.sweep_id is not None else {}
        total = sum(of.values())
        open_ = of.get(BacktestStatus.QUEUED, 0) + of.get(BacktestStatus.RUNNING, 0)
        items.append(
            TemplateItemOut(
                id=item.id,
                symbol=item.symbol,
                cost_model=dict(item.cost_model),
                position=item.position,
                status=item.status.value,
                sweep_id=item.sweep_id,
                error=item.error,
                runs=total,
                done=of.get(BacktestStatus.DONE, 0),
                failed=of.get(BacktestStatus.FAILED, 0),
                finished=item.sweep_id is not None and total > 0 and open_ == 0,
            )
        )
    return SweepTemplateOut(
        id=template.id,
        name=template.name,
        entry_ids=[uuid.UUID(one) for one in template.entry_ids],
        entry_names=[names.get(one) for one in template.entry_ids],
        timeframes=list(template.timeframes),
        date_from=template.date_from,
        date_to=template.date_to,
        initial_capital=template.initial_capital,
        paused=template.paused,
        created_at=template.created_at,
        items=items,
    )


__all__ = ["router", "template_out"]
