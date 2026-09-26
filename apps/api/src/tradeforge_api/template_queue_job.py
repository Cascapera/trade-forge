"""A template's queue: one market's sweep at a time, the next when the last has ended (26/09).

Called by the worker job `run_template_queue`, again every minute while a market is running or
waiting. Never two markets at once: that is the point of the queue — a day's worth of markets run
one after the other, each light enough to read while the next is running.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.routers.sweeps import launch_sweep
from tradeforge_api.schemas import CreateSweep
from tradeforge_api.sweep_walkforward_job import settled
from tradeforge_db.models import (
    Collection,
    SweepTemplate,
    SweepTemplateItem,
    TemplateItemStatus,
)


def advance_queue(
    session: Session, template_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[Collection], bool]:
    """Launch the next market if nothing of this template is running; what to queue, and whether
    to look again.

    ⚠️ **The template row is locked while deciding**, so two calls at once — a market added
    while the conductor was already due — cannot both launch. The item is marked launched in the
    same transaction the sweep is written in (`launch_sweep` commits both), so no reader ever sees
    a sweep without its item or an item launched without its sweep.

    ⚠️ **A refused launch fails that market and the queue goes on** — an unknown symbol, a window
    with no candles for it — with the reason on the item.
    """
    template = session.execute(
        select(SweepTemplate).where(SweepTemplate.id == template_id).with_for_update()
    ).scalar_one_or_none()
    if template is None:
        return [], [], False

    for item in template.items:
        if (
            item.status is TemplateItemStatus.LAUNCHED
            and item.sweep_id is not None
            and not settled(session, item.sweep_id)
        ):
            session.commit()
            return [], [], True
    if template.paused:
        session.commit()
        return [], [], False

    waiting = [one for one in template.items if one.status is TemplateItemStatus.WAITING]
    for item in waiting:
        item_id = item.id
        request = CreateSweep(
            entry_ids=[uuid.UUID(one) for one in template.entry_ids],
            symbols=[item.symbol],
            timeframes=list(template.timeframes),
            date_from=template.date_from,
            date_to=template.date_to,
            initial_capital=template.initial_capital,
            cost_model={"type": "per_market", "markets": {item.symbol: dict(item.cost_model)}},
        )
        item.status = TemplateItemStatus.LAUNCHED
        try:
            sweep, runs, collections, _shared, _skipped = launch_sweep(
                session, request, template_id=template.id
            )
        except HTTPException as refused:
            session.rollback()
            failed = session.get(SweepTemplateItem, item_id)
            assert failed is not None  # noqa: S101 — it was read above under the lock
            failed.status = TemplateItemStatus.FAILED
            failed.error = str(refused.detail)
            session.commit()
            return advance_queue(session, template_id)
        launched = session.get(SweepTemplateItem, item_id)
        assert launched is not None  # noqa: S101 — committed with the sweep just now
        launched.sweep_id = sweep.id
        session.commit()
        return runs, collections, True

    session.commit()
    return [], [], False


__all__ = ["advance_queue"]
