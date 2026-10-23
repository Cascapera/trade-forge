"""FastAPI dependencies: the per-request database session and the shared arq pool.

Everything a handler needs is reached through `request.app.state`, populated once at startup
(see `main.create_app`). The session is opened per request and closed in a `finally`, and it
is *not* committed here — a handler commits explicitly when it has written something, so a
read path never issues a needless transaction.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from tradeforge_api.collector import Collector
from tradeforge_api.config import Settings
from tradeforge_api.kill_switch import KillSwitch
from tradeforge_api.live.stop import StopStore
from tradeforge_api.queue import JobQueue
from tradeforge_api.snapshot_store import SnapshotStore


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    db: Session = factory()
    try:
        yield db
    finally:
        db.close()


def get_queue(request: Request) -> JobQueue:
    pool: JobQueue = request.app.state.arq_pool
    return pool


def get_collector(request: Request) -> Collector:
    collector: Collector = request.app.state.collector
    return collector


def get_kill_switch(request: Request) -> KillSwitch:
    switch: KillSwitch = request.app.state.kill_switch
    return switch


def get_stop_store(request: Request) -> StopStore:
    store: StopStore = request.app.state.stop_store
    return store


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]
CollectorDep = Annotated[Collector, Depends(get_collector)]
KillSwitchDep = Annotated[KillSwitch, Depends(get_kill_switch)]
StopStoreDep = Annotated[StopStore, Depends(get_stop_store)]


def get_snapshots(request: Request) -> SnapshotStore:
    store: SnapshotStore = request.app.state.snapshots
    return store


SnapshotsDep = Annotated[SnapshotStore, Depends(get_snapshots)]
