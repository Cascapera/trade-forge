"""`/instruments` — the catalogue of what can be traded."""

from fastapi import APIRouter
from sqlalchemy import select

from tradeforge_api.deps import SessionDep
from tradeforge_api.schemas import InstrumentOut
from tradeforge_db.models import Instrument

router = APIRouter(tags=["instruments"])


@router.get("/instruments", response_model=list[InstrumentOut])
def list_instruments(session: SessionDep) -> list[Instrument]:
    """Every tradable symbol, ordered by name so the list is stable between calls."""
    return list(session.scalars(select(Instrument).order_by(Instrument.symbol)))
