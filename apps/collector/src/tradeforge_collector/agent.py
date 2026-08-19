"""The host agent: the only process that may answer a question about MetaTrader.

`arq tradeforge_collector.agent.WorkerSettings` runs on the **Windows host**, beside the
terminal, and drains a queue no container ever touches. The API drops jobs on that queue and
reads the results out of Postgres; it never learns that MetaTrader exists.

⚠️ **This is topology, not preference.** The `MetaTrader5` wheel is Windows-only, so the API
and the backtest worker — which run in Linux containers — cannot import it even if ADR-02
allowed them to, and `uv sync --locked` on Linux CI proves that on every push. Something has to
run on the Windows side; this is that something, and putting it in `apps/collector` keeps the
broker edge exactly where ADR-02 drew it.

## Why a second queue rather than a second function on the existing one

Two reasons, and either alone would be enough:

* **Capability.** A container physically cannot execute these jobs. With one shared queue, a
  Linux worker would happily claim `sync_symbols` and fail — or worse, the job would sit
  `queued` for ever with nothing raised. Segregating makes "who is able to run this" a property
  of the queue instead of a hope about which worker gets there first.
* **Latency.** Photographing a symbol list is a sub-second job behind a click somebody is
  watching. Sharing a queue with a walk-forward would put it behind twenty minutes of
  CPU-bound backtesting.

## Why the connection is per job and not per worker

A long-lived `MT5Source` held across jobs would be a connection this process cannot notice
losing: the terminal gets restarted, the handle goes stale, and every job from then on fails
until somebody restarts the agent. Jobs here are seconds long and `initialize()` costs
milliseconds, so attaching per job buys freshness for nothing. The live loop makes the opposite
trade for the opposite reason — it holds one connection for days and therefore has to detect
and repair a lost one (`live.reconnect`).
"""

import datetime as dt
import logging
import os
from typing import Any

from arq.connections import RedisSettings

from tradeforge_collector.source import SymbolInfo
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope

__all__ = ["COLLECT_QUEUE", "WorkerSettings", "redis_settings_from_env", "sync_symbols"]

logger = logging.getLogger(__name__)

COLLECT_QUEUE = "collect"
"""⚠️ Must match `tradeforge_api.queue.COLLECT_QUEUE`, and `tests/test_queue_contract.py` is
what makes that true rather than hoped. The two apps do not import each other — the queue name
and the job name *are* the interface between them, and an interface spelled out twice needs
something asserting the spellings agree."""


def _entry(info: SymbolInfo) -> BrokerSymbolEntry:
    """The adapter's shallow view of a symbol, as the row the catalogue stores.

    Two nearly identical dataclasses with a function between them, on purpose: `SymbolInfo` is
    what a *source* can report and `BrokerSymbolEntry` is what the *database* stores, and they
    are free to drift. It is the same seam as `InstrumentSpec` → `CatalogueEntry` next door,
    and it is what keeps `mt5_source.py` from importing `tradeforge_db`.
    """
    return BrokerSymbolEntry(
        symbol=info.symbol,
        description=info.description,
        path=info.path,
        digits=info.digits,
        visible=info.visible,
    )


async def sync_symbols(_context: dict[str, Any]) -> int:
    """Photograph the broker's catalogue into `broker_symbols`. Returns how many rows landed.

    ⚠️ **The whole snapshot, in one transaction, replacing what was there.** Merging would grow
    a list that is the union of every broker ever connected, so switching accounts would leave
    the screen offering symbols the current account cannot trade — and this terminal has
    already gone from 9550 symbols with AAPL in them to 84 of forex and CFDs.

    An empty catalogue is refused by `replace_snapshot`, not tolerated here: a terminal that
    lists nothing is a terminal that is not logged in, and wiping the list on that reading
    would take search down whenever the sync happened to run at a bad moment.

    The `arq` context is accepted and ignored. It is the runner's calling convention, and
    naming it `_context` says the job takes nothing from the caller — every input it has comes
    from the terminal it is standing next to.
    """
    # Imported inside the job, exactly like `cli._source` does it: at module scope this line
    # would make the file unimportable on Linux, and `tests/test_queue_contract.py` imports it
    # from Linux CI to check the job names line up (ADR-02).
    from tradeforge_collector.mt5_source import MT5Source  # noqa: PLC0415 — the ADR-02 boundary

    source = MT5Source(server_offset=_stated_offset()).connect()
    try:
        symbols = source.symbols()
        server = source.server()
    finally:
        source.close()

    engine = create_db_engine()
    try:
        with session_scope(create_session_factory(engine)) as session:
            count = replace_snapshot(
                session,
                [_entry(info) for info in symbols],
                server=server,
                synced_at=dt.datetime.now(tz=dt.UTC),
            )
    finally:
        engine.dispose()

    logger.info("synced %d symbols from %s", count, server or "an unnamed server")
    return count


def _stated_offset() -> dt.timedelta | None:
    """`TRADEFORGE_SERVER_OFFSET` in hours, or `None` to let the source measure it.

    ⚠️ Worth stating in the agent's environment even though *this* job never labels a bar.
    Connecting measures the broker's clock when no offset is given, and that measurement is
    refused while the market is shut (`_measure_offset`) — so an agent without it would fail to
    connect all weekend, which is exactly when somebody sits down to look for a new symbol.
    """
    raw = os.environ.get("TRADEFORGE_SERVER_OFFSET")
    if raw is None:
        return None
    try:
        return dt.timedelta(hours=float(raw))
    except ValueError:
        raise ValueError(
            f"TRADEFORGE_SERVER_OFFSET must be hours ahead of UTC, like +3, got {raw!r}"
        ) from None


def redis_settings_from_env() -> RedisSettings:
    """Where Redis is, read from the environment because this process is outside compose.

    The containers get `REDIS_HOST: redis` from the compose file; this agent runs on the host,
    where the same server is `localhost` on the published port. The defaults are the host case,
    so the ordinary invocation needs no environment at all.
    """
    return RedisSettings(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
    )


class WorkerSettings:
    """`arq tradeforge_collector.agent.WorkerSettings` starts the host agent from this."""

    functions = (sync_symbols,)
    queue_name = COLLECT_QUEUE

    # ⚠️ One job at a time. The terminal is a single shared resource with one IPC channel, and
    # two jobs asking it for history at once is not twice as fast — the measured cost of a cold
    # history request is the terminal downloading, and parallel requests contend for the same
    # download. Serial also means a sync can never observe a half-written snapshot.
    max_jobs = 1

    # ⚠️ **A value, not a method.** arq *reads* this attribute; it never calls it. The first
    # version here was a `@staticmethod`, which a test could call and assert on quite happily
    # while `arq` blew up on `'staticmethod' object has no attribute 'host'` the moment the
    # worker actually started. Evaluated at import, exactly like the API's worker does it.
    redis_settings = redis_settings_from_env()
