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

from tradeforge_api.schemas import UncoveredMarket
from tradeforge_db.models import Dataset, Instrument


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
