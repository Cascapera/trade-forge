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
import uuid
from pathlib import Path
from typing import Any

from arq.connections import RedisSettings

from tradeforge_collector.collect import DatabaseJournal, run_collection
from tradeforge_collector.source import SymbolInfo
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.collections import read_collection
from tradeforge_db.session import create_db_engine, create_session_factory, session_scope
from tradeforge_db.symbol_history import HistoryProbe, upsert_history

__all__ = [
    "COLLECT_QUEUE",
    "WorkerSettings",
    "collect_range",
    "data_root_from_env",
    "probe_history",
    "redis_settings_from_env",
    "sync_symbols",
]

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


async def probe_history(_context: dict[str, Any], symbol: str, timeframe: str) -> int:
    """Measure how much history one series really offers. Returns the bar count.

    ⚠️ **This is the slow job, and unavoidably so.** Measured on this broker, a cold H4 took
    **207 seconds** — the terminal downloads the history while answering. That is the whole
    reason the API returns 202 and the screen polls instead of waiting.

    Takes its arguments rather than reading a list, unlike `sync_symbols`: probing every symbol
    on every timeframe would be 84 x 8 cold downloads, and the screen only ever needs the one a
    person is looking at.
    """
    from tradeforge_collector.mt5_source import MT5Source  # noqa: PLC0415 — the ADR-02 boundary

    source = MT5Source(server_offset=_stated_offset()).connect()
    try:
        report = source.probe_history(symbol, timeframe)
    finally:
        source.close()

    engine = create_db_engine()
    try:
        with session_scope(create_session_factory(engine)) as session:
            upsert_history(
                session,
                HistoryProbe(
                    symbol=symbol,
                    timeframe=timeframe,
                    oldest=report.oldest,
                    bar_count=report.bar_count,
                    terminal_maxbars=report.terminal_maxbars,
                    bar_count_is_a_ceiling=report.bar_count_is_a_ceiling,
                    last_fabricated=report.last_fabricated,
                    first_measured_cost=report.first_measured_cost,
                ),
                probed_at=dt.datetime.now(tz=dt.UTC),
            )
    finally:
        engine.dispose()

    logger.info(
        "probed %s %s: %d bars from %s (filler to %s, costs measured from %s)",
        symbol,
        timeframe,
        report.bar_count,
        report.oldest,
        report.last_fabricated,
        report.first_measured_cost,
    )
    return report.bar_count


async def collect_range(_context: dict[str, Any], collection_id: str) -> int:
    """Download the range one `collections` row asked for. Returns how many bars are on disk.

    ⚠️ **The queue carries an id and nothing else, and that is the point.** Symbol, timeframe,
    window and asset class are all already on the row the API wrote before answering 202 — the
    same row `GET /collections/{id}` reports and the screen renders. A payload repeating them
    would be a second copy free to disagree with the one a person is looking at, and the
    disagreement would be invisible: the screen would show the window it stored while the agent
    downloaded the window it was handed.

    ⚠️ **Four lines of MetaTrader and nothing else.** Everything that can be gotten wrong —
    where the range is cut, what an empty year means, what the catalogue should claim — lives in
    `collect.run_collection`, which takes a source and a journal and therefore runs against
    `SyntheticSource` on Linux CI. This function is the part that cannot be tested there, so it
    is kept down to the part that cannot be tested there.
    """
    from tradeforge_collector.mt5_source import MT5Source  # noqa: PLC0415 — the ADR-02 boundary

    root = data_root_from_env()
    engine = create_db_engine()
    try:
        session = create_session_factory(engine)()
        try:
            request = read_collection(session, uuid.UUID(collection_id))
            if request is None:
                # Deleted between enqueue and pickup. Nothing to run and nothing to record —
                # the row that would have held the error is the row that is gone.
                logger.warning("collection %s no longer exists", collection_id)
                return 0

            # ⚠️ The class the requester supplied, or None to let the path decide. It reaches
            # the source rather than the database because `instrument()` is what needs it: 24
            # of this broker's 84 symbols file under roots `classify` refuses to guess at, and
            # without an override those symbols cannot be catalogued at all.
            source = MT5Source(
                server_offset=_stated_offset(), asset_class=request.asset_class
            ).connect()
            try:
                outcome = run_collection(
                    source,
                    DatabaseJournal(
                        session,
                        request.id,
                        root=root,
                        timeframe=request.timeframe,
                    ),
                    root=root,
                    symbol=request.symbol,
                    timeframe=request.timeframe,
                    date_from=request.date_from,
                    date_to=request.date_to,
                )
            finally:
                source.close()

            logger.info(
                "collected %s %s: %d candles over %d of %d years",
                request.symbol,
                request.timeframe,
                outcome.candles,
                outcome.slices_with_data,
                outcome.slices_total,
            )
            return outcome.candles
        finally:
            session.close()
    finally:
        engine.dispose()


def data_root_from_env() -> Path:
    """Where the Parquet goes, read from the environment because this process is outside compose.

    ⚠️ **The host is the only writer, and the compose file enforces that**: `./data` is bind
    mounted into the API and the worker as `:ro`. The containers read candles they cannot
    create, which is the same boundary as ADR-02 seen from the filesystem — the process that
    can talk to MetaTrader is the process that owns the files MetaTrader produced.

    Defaults to the path the CLI has always used, so the ordinary invocation from the repo root
    needs no environment at all.
    """
    return Path(os.environ.get("TRADEFORGE_DATA_DIR", "data/ohlcv"))


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

    functions = (sync_symbols, probe_history, collect_range)
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
