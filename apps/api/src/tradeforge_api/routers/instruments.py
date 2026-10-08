"""`/instruments` — the catalogue of what can be traded."""

from fastapi import APIRouter
from sqlalchemy import select

from tradeforge_api.deps import SessionDep
from tradeforge_api.schemas import InstrumentOut
from tradeforge_db.models import Broker, Instrument

router = APIRouter(tags=["instruments"])


@router.get("/instruments", response_model=list[InstrumentOut])
def list_instruments(session: SessionDep) -> list[InstrumentOut]:
    """Every tradable symbol, ordered by name so the list is stable between calls, with the
    broker it is collected from (ADR-0032) — an outer join, so a seed with none still shows."""
    rows = session.execute(
        select(Instrument, Broker.slug)
        .outerjoin(Broker, Broker.id == Instrument.broker_id)
        .order_by(Instrument.symbol)
    )
    return [
        InstrumentOut.model_validate(instrument).model_copy(update={"broker": broker})
        for instrument, broker in rows
    ]
