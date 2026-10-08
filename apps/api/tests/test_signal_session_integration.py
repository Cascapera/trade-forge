"""A whole SIGNAL session over real Postgres: paper's own trades, each told as signals (PR 5)."""

# The paper suite's fixtures are imported by name, which is how pytest finds them; each test then
# takes them as parameters, which ruff reads as redefining the import.
# ruff: noqa: F811

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.live.session import SessionPlan, run_session
from tradeforge_api.live.signals import SignalEvent, SignalKindOf
from tradeforge_db.models import SessionMode, Trade
from tradeforge_engine.errors import EngineError

from .test_session_integration import (  # noqa: F401 — fixtures
    CUT,
    ListSource,
    factory_of,
    parquet_root,
    plan,
    rows,
    split_at,
)

pytestmark = pytest.mark.integration


class Sink:
    def __init__(self) -> None:
        self.events: list[SignalEvent] = []
        self._next = 0

    def emit(self, event: SignalEvent) -> None:
        self.events.append(event)

    def number(self) -> int:
        self._next += 1
        return self._next


def test_every_trade_the_session_records_is_told_as_a_signal(
    session: Session,
    session_factory: Callable[[], Session],
    plan: SessionPlan,
    parquet_root: Path,
) -> None:
    history, live_bars, cut = split_at(CUT)
    sink = Sink()

    outcome = run_session(
        factory=factory_of(session_factory),
        source=ListSource(history, live_bars),
        plan=dataclasses.replace(plan, mode=SessionMode.SIGNAL),
        parquet_root=parquet_root,
        stopping=lambda: False,
        now=lambda: cut,
        signals=sink,
    )

    assert outcome.error is None
    recorded = session.scalars(
        select(Trade).where(Trade.live_session_id == outcome.session_id)
    ).all()
    closed = [event for event in sink.events if event.kind is SignalKindOf.CLOSED]
    assert recorded, "the golden bars trade once inside the session"
    assert [event.result_r for event in closed] == [trade.r_multiple for trade in recorded]
    # Each closed signal was triggered first, under the same number.
    triggered = {e.number for e in sink.events if e.kind is SignalKindOf.TRIGGERED}
    assert {event.number for event in closed} <= triggered


def test_a_signal_session_with_nowhere_to_post_is_refused_before_it_starts(
    session_factory: Callable[[], Session], plan: SessionPlan, parquet_root: Path
) -> None:
    history, live_bars, cut = split_at(CUT)

    with pytest.raises(EngineError, match="nowhere to post"):
        run_session(
            factory=factory_of(session_factory),
            source=ListSource(history, live_bars),
            plan=dataclasses.replace(plan, mode=SessionMode.SIGNAL),
            parquet_root=parquet_root,
            stopping=lambda: False,
            now=lambda: cut,
        )
