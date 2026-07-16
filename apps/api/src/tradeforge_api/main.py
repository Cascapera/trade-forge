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
from sqlalchemy.orm import Session

from tradeforge_api import __version__, ws
from tradeforge_api.config import Settings
from tradeforge_api.deps import SettingsDep
from tradeforge_api.health import check_postgres, check_redis
from tradeforge_api.queue import JobQueue, redis_settings
from tradeforge_api.routers import backtests, instruments, strategies
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

    try:
        yield
    finally:
        if getattr(app.state, "_owns_pool", False):
            await app.state.arq_pool.aclose()
        if owns_engine:
            app.state._engine.dispose()


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
    arq_pool: JobQueue | None = None,
) -> FastAPI:
    """Build the app. Pass `session_factory`/`arq_pool` to bypass the real connections."""
    app = FastAPI(title="TradeForge API", version=__version__, lifespan=_lifespan)
    app.state.settings = settings or Settings()
    if session_factory is not None:
        app.state.session_factory = session_factory
    if arq_pool is not None:
        app.state.arq_pool = arq_pool

    app.include_router(instruments.router)
    app.include_router(strategies.router)
    app.include_router(backtests.router)
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
