"""The FastAPI application factory.

`create_app` wires the routers to their dependencies and manages the two long-lived resources
the API holds: a database session factory and the arq queue pool. Both are created in the
lifespan and torn down with it — *unless* they were injected, which is the seam the tests use
to run the whole HTTP surface against fakes, with no Postgres or Redis anywhere.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict

from arq import create_pool
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy.orm import Session

from tradeforge_api import __version__, ws
from tradeforge_api.config import Settings
from tradeforge_api.deps import SettingsDep
from tradeforge_api.health import check_postgres, check_redis
from tradeforge_api.kill_switch import KillSwitch
from tradeforge_api.live.stop import StopStore
from tradeforge_api.queue import JobQueue, redis_settings
from tradeforge_api.routers import (
    backtests,
    baskets,
    collections,
    executor,
    instruments,
    live_sessions,
    strategies,
    studies,
    symbols,
    walkforwards,
)
from tradeforge_db.session import create_db_engine, create_session_factory


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create only what was not injected, and dispose only what we created."""
    settings: Settings = app.state.settings
    owns_engine = False

    if not hasattr(app.state, "session_factory"):
        engine = create_db_engine(settings.sqlalchemy_dsn)
        app.state.session_factory = create_session_factory(engine)
        app.state._engine = engine
        owns_engine = True

    if not hasattr(app.state, "arq_pool"):
        app.state.arq_pool = await create_pool(redis_settings(settings))
        app.state._owns_pool = True

    if not (hasattr(app.state, "kill_switch") and hasattr(app.state, "stop_store")):
        # ⚠️ A client of its own rather than the arq pool, and the reason is not tidiness. The
        # pool is async and arq's; these are read and written by plain handlers, and an emergency
        # path that borrows the job queue's connection is an emergency path that stops working
        # the day the queue is reconfigured. `decode_responses` so the timestamps come back as
        # text — neither key is ever read for its value in order to *decide* anything.
        #
        # ⚠️ These lines are also the proof that `SwitchStore` and `StopStore` describe the real
        # client: mypy has a concrete `Redis` to check the protocols against here, and nowhere
        # else. One client, two mechanisms — deliberately not one mechanism, because the kill
        # switch fails closed and a stop request fails open (see `live/stop.py`).
        app.state._redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    if not hasattr(app.state, "kill_switch"):
        app.state.kill_switch = KillSwitch(app.state._redis_client)
    if not hasattr(app.state, "stop_store"):
        app.state.stop_store = app.state._redis_client

    try:
        yield
    finally:
        if getattr(app.state, "_owns_pool", False):
            await app.state.arq_pool.aclose()
        if getattr(app.state, "_redis_client", None) is not None:
            app.state._redis_client.close()
        if owns_engine:
            app.state._engine.dispose()


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
    arq_pool: JobQueue | None = None,
    kill_switch: KillSwitch | None = None,
    stop_store: StopStore | None = None,
) -> FastAPI:
    """Build the app. Pass `session_factory`/`arq_pool`/`kill_switch` to bypass the real
    connections.

    ⚠️ **`kill_switch` and `stop_store` are the seams a test has to use on purpose.** The other
    two exist so tests can run with no Postgres and no Redis; these exist because a test that
    runs *with* a real Redis would otherwise write keys the executor and the live sessions read.
    A fuzzer pointed at this app finds `POST /executor/kill-switch` and presses it — see
    `test_schemathesis_integration`, which injects fakes for exactly that reason. The failure it
    prevents is silent and expensive: a key left behind after a test run halts the next live
    session with no error anywhere.
    """
    app = FastAPI(title="TradeForge API", version=__version__, lifespan=_lifespan)
    app.state.settings = settings or Settings()
    if session_factory is not None:
        app.state.session_factory = session_factory
    if arq_pool is not None:
        app.state.arq_pool = arq_pool
    if kill_switch is not None:
        app.state.kill_switch = kill_switch
    if stop_store is not None:
        app.state.stop_store = stop_store

    app.include_router(instruments.router)
    app.include_router(symbols.router)
    app.include_router(collections.router)
    app.include_router(strategies.router)
    app.include_router(backtests.router)
    app.include_router(executor.router)
    app.include_router(live_sessions.router)
    app.include_router(baskets.router)
    app.include_router(studies.router)
    app.include_router(walkforwards.router)
    app.include_router(ws.router)

    @app.get("/health", tags=["health"])
    def health(settings: SettingsDep) -> JSONResponse:
        services = [
            check_postgres(settings.postgres_dsn),
            check_redis(settings.redis_url),
        ]
        ok = all(service.ok for service in services)
        code = 200 if ok else 503
        return JSONResponse(status_code=code, content={"services": [asdict(s) for s in services]})

    return app


__all__ = ["create_app"]
