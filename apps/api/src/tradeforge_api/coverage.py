"""Which markets hold no candles inside a window — asked of the index before a run is queued.

⚠️ **One module, because three launches ask the same question.** The sweep (preview and
launch) asked it first; the single backtest and the basket ask it too since PR-262, and the
answer has to read the same wherever it appears, so the sentence that describes a missing market
lives here beside the query.

⚠️ **"No candles at all" is the only refusal.** A window the data covers in part is a real
measurement over the part that exists, and a run reports what it read as its first and last
candle. Planning the missing part for collection is `collection_plan`'s question, not this one.
"""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.collection_plan import Window, missing_windows
from tradeforge_api.schemas import PlannedCollection, PlannedWindow, UncoveredMarket
from tradeforge_collector import step
from tradeforge_db.models import BrokerSymbol, Dataset, Instrument, SymbolHistory


def uncovered_markets(
    session: Session,
    symbols: list[str],
    timeframes: list[str],
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> list[UncoveredMarket]:
    """Which (symbol, timeframe) pairs hold no candles inside this window.

    ⚠️ **Asked of the index, not of the files.** `datasets` exists to answer "do I have EURUSD H1
    for 2021?" with a lookup rather than by opening Parquet (ADR-05), and that is the whole
    reason this check is cheap enough to run before a launch.

    ⚠️ **This exists because the first real sweep lost nine runs of twelve to it.** Each failure
    was honest — the worker said exactly which window the data covers — but it arrived after
    twelve jobs had been enqueued, which is the round trip a preview exists to remove. A pair
    that has never been collected and a pair collected for other years are reported apart: one
    is a backfill to run, the other is a window to move.
    """
    instruments = {
        instrument.symbol: instrument
        for instrument in session.scalars(select(Instrument).where(Instrument.symbol.in_(symbols)))
    }
    coverage = {
        (row.instrument_id, row.timeframe): row
        for row in session.scalars(
            select(Dataset).where(
                Dataset.instrument_id.in_([one.id for one in instruments.values()]),
                Dataset.timeframe.in_(timeframes),
            )
        )
    }

    out: list[UncoveredMarket] = []
    for symbol in symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            continue  # an unknown symbol is a different refusal, and the caller makes it first
        for timeframe in timeframes:
            dataset = coverage.get((instrument.id, timeframe))
            if dataset is None:
                out.append(UncoveredMarket(symbol=symbol, timeframe=timeframe, covers=None))
            # ⚠️ Closed at both ends, like the worker's read (`date_from <= time <= date_to`):
            # a window opening exactly on the last bar reads that bar.
            elif dataset.date_from > date_to or dataset.date_to < date_from:
                # No overlap at all. A *partial* overlap is deliberately allowed: a run over the
                # half of the window that exists is a real measurement, and refusing it would
                # make every sweep wait for the least-collected symbol on the list.
                out.append(
                    UncoveredMarket(
                        symbol=symbol,
                        timeframe=timeframe,
                        covers=(
                            f"{dataset.date_from.date().isoformat()} to "
                            f"{dataset.date_to.date().isoformat()}"
                        ),
                    )
                )
    return out


def describe(market: UncoveredMarket) -> str:
    """`EURUSD M15 (never collected)` or `EURUSD M15 (on disk: 2020-01-02 to 2026-09-10)`."""
    held = "never collected" if market.covers is None else f"on disk: {market.covers}"
    return f"{market.symbol} {market.timeframe} ({held})"


def plan_for(
    session: Session,
    *,
    symbols: list[str],
    timeframes: list[str],
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> list[PlannedCollection]:
    """Which (symbol, timeframe) pairs still need collecting for a run over this window.

    ⚠️ **One function, because the plan a person is shown and the plan a launch acts on must be
    the same one.** `POST /collections/plan` answers with it; a launch told to collect first
    queues exactly these windows. Two implementations would differ the first time either changed,
    and the difference would read as the screen lying about what it was about to download.

    Nothing is written here. The rules — whole years, windows that reach the data on disk, slack
    at the edges, nothing before the broker's oldest bar — are `collection_plan`'s.
    """
    now = dt.datetime.now(tz=dt.UTC)
    extents = {
        (symbol, timeframe): Window(date_from, date_to)
        for symbol, timeframe, date_from, date_to in session.execute(
            select(Instrument.symbol, Dataset.timeframe, Dataset.date_from, Dataset.date_to)
            .join(Instrument, Instrument.id == Dataset.instrument_id)
            .where(
                Instrument.symbol.in_(symbols),
                Dataset.timeframe.in_(timeframes),
            )
        )
    }
    # ⚠️ A probe that found no bars at all stores NULL, and reads back here exactly like a pair
    # never probed: both plan the whole window. For the first, the collection then fails with a
    # sentence on the screen rather than being planned away in silence — but asked again, the
    # plan asks again. Whatever queues from this plan must stop on a collection that already
    # came back empty, or it loops.
    oldest = {
        (symbol, timeframe): first
        for symbol, timeframe, first in session.execute(
            select(SymbolHistory.symbol, SymbolHistory.timeframe, SymbolHistory.oldest).where(
                SymbolHistory.symbol.in_(symbols),
                SymbolHistory.timeframe.in_(timeframes),
            )
        )
    }

    # The launch's own question, asked here so the screen never has to guess it from `covers`.
    empty = {
        (market.symbol, market.timeframe)
        for market in uncovered_markets(
            session,
            list(symbols),
            list(timeframes),
            date_from,
            date_to,
        )
    }

    # ⚠️ **"Not listed" only means something once the list exists.** The snapshot is replaced
    # whole and never emptied (`replace_snapshot` refuses an empty one), so an empty table is a
    # sync that has never run — "I do not know" — and every symbol stays `None`, offered as before.
    # The snapshot is `symbols_get()` in full, not the Market Watch: absent from it is absent from
    # the broker.
    synced = session.scalar(select(BrokerSymbol.id).limit(1)) is not None
    listed = set(
        session.scalars(select(BrokerSymbol.symbol).where(BrokerSymbol.symbol.in_(symbols)))
    )

    out: list[PlannedCollection] = []
    for symbol in symbols:
        for timeframe in timeframes:
            on_disk = extents.get((symbol, timeframe))
            windows = missing_windows(
                date_from=date_from,
                date_to=date_to,
                on_disk=on_disk,
                oldest=oldest.get((symbol, timeframe)),
                now=now,
                bar=step(timeframe),
            )
            if not windows:
                continue
            out.append(
                PlannedCollection(
                    symbol=symbol,
                    timeframe=timeframe,
                    # An instrument the table does not know is not in `empty` — it was never
                    # asked about — and holds no candle either.
                    in_window=on_disk is not None and (symbol, timeframe) not in empty,
                    covers=(
                        None
                        if on_disk is None
                        else f"{on_disk.date_from.date().isoformat()} to "
                        f"{on_disk.date_to.date().isoformat()}"
                    ),
                    windows=[
                        PlannedWindow(date_from=w.date_from, date_to=w.date_to) for w in windows
                    ],
                    at_broker=(symbol in listed) if synced else None,
                )
            )
    return out


def to_collect(
    session: Session,
    *,
    symbols: list[str],
    timeframes: list[str],
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> list[PlannedCollection]:
    """What a launch told to collect actually downloads: the plan, less what the broker lacks.

    ⚠️ **The one filter every launch goes through.** The single backtest, the basket and the
    sweep each turned the plan into collections on their own; a filter written into each would be
    forgotten in the next one. A pair the broker does not list stays out of this list, so no
    collection is written for it and the launch treats it as any pair with nothing to fetch —
    the basket and the sweep skip and name it, the single backtest is refused.

    The screen's plan (`POST /collections/plan`) is `plan_for` itself, unfiltered: the person
    must be told a pair cannot be collected, not find it silently missing from the list.
    """
    return [
        market
        for market in plan_for(
            session,
            symbols=symbols,
            timeframes=timeframes,
            date_from=date_from,
            date_to=date_to,
        )
        if market.at_broker is not False
    ]
