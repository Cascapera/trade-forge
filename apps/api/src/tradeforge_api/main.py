"""The FastAPI application factory.

`create_app` wires the routers to their dependencies and manages the two long-lived resources
the API holds: a database session factory and the arq queue pool. Both are created in the
lifespan and torn down with it — *unless* they were injected, which is the seam the tests use
to run the whole HTTP surface against fakes, with no Postgres or Redis anywhere.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Executor
from contextlib import asynccontextmanager
from dataclasses import asdict

from arq import create_pool
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy.orm import Session

from tradeforge_api import __version__, ws
from tradeforge_api.collector import Collector
from tradeforge_api.config import Settings
from tradeforge_api.deps import SettingsDep
from tradeforge_api.health import check_postgres, check_redis
from tradeforge_api.kill_switch import KillSwitch
from tradeforge_api.live.stop import StopStore
from tradeforge_api.queue import JobQueue, redis_settings
from tradeforge_api.routers import (
    backtests,
    baskets,
    best,
    candle_files,
    catalog,
    clusters,
    collections,
    executor,
    instruments,
    live_sessions,
    live_setups,
    strategies,
    studies,
    sweep_templates,
    sweep_walkforwards,
    sweeps,
    symbols,
    walkforwards,
)
from tradeforge_api.snapshot_store import InMemory, SnapshotStore
from tradeforge_api.snapshots import child_pool, refresh_forever
from tradeforge_db.session import create_db_engine, create_session_factory


def _start_snapshots(
    app: FastAPI, settings: Settings, *, refresh: bool
) -> tuple["asyncio.Task[None] | None", Executor | None]:
    """The heavy pages' snapshot store, and their background refresh (05/10) — only where the
    app made its own connections: a test that injected them gets no loop reaching a database."""
    if not hasattr(app.state, "snapshots"):
        # ⚠️ Kept in Redis only beside a database this app opened. A test hands the app its own
        # database but reaches the Redis of the system in use: a map kept there would replace the
        # real one, and a test would read the map another test left.
        if not refresh:
            app.state.snapshots = SnapshotStore(InMemory())
        else:
            if getattr(app.state, "_redis_client", None) is None:
                app.state._redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
            app.state.snapshots = SnapshotStore(app.state._redis_client)
    if not refresh:
        return None, None
    pool = child_pool()
    task = asyncio.create_task(
        refresh_forever(app.state.session_factory, app.state.snapshots, pool)
    )
    return task, pool


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

    if not (
        hasattr(app.state, "kill_switch")
        and hasattr(app.state, "stop_store")
        and hasattr(app.state, "collector")
    ):
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
    if not hasattr(app.state, "collector"):
        app.state.collector = Collector(app.state._redis_client)
    refreshing, pool = _start_snapshots(app, settings, refresh=owns_engine)

    try:
        yield
    finally:
        if refreshing is not None:
            refreshing.cancel()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        if getattr(app.state, "_owns_pool", False):
            await app.state.arq_pool.aclose()
        if getattr(app.state, "_redis_client", None) is not None:
            app.state._redis_client.close()
        if owns_engine:
            app.state._engine.dispose()


def create_app(  # noqa: PLR0913 — keyword-only seams, one per connection a test replaces
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
    arq_pool: JobQueue | None = None,
    kill_switch: KillSwitch | None = None,
    stop_store: StopStore | None = None,
    collector: Collector | None = None,
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
    if collector is not None:
        app.state.collector = collector

    app.include_router(instruments.router)
    app.include_router(candle_files.router)
    app.include_router(best.router)
    app.include_router(symbols.router)
    app.include_router(collections.router)
    app.include_router(strategies.router)
    app.include_router(catalog.router)
    app.include_router(backtests.router)
    app.include_router(executor.router)
    app.include_router(live_sessions.router)
    app.include_router(baskets.router)
    app.include_router(studies.router)
    # Before `sweeps`: `/sweeps/{id}/walkforward` must not be read by a broader sweeps path.
    app.include_router(sweep_walkforwards.router)
    app.include_router(sweep_templates.router)
    app.include_router(sweeps.router)
    app.include_router(walkforwards.router)
    app.include_router(clusters.router)
    app.include_router(live_setups.router)
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
