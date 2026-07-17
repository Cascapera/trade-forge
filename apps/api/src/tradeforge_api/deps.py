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

from tradeforge_api.config import Settings
from tradeforge_api.queue import JobQueue


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


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
QueueDep = Annotated[JobQueue, Depends(get_queue)]
