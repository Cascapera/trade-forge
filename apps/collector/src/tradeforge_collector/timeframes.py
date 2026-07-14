"""How long a bar lasts.

The set of legal timeframes is owned by the DSL (`tradeforge_schema.TIMEFRAMES`).
This module only says how much wall-clock time each one spans — which is what gap
detection needs to know what "the next candle" would have been.
"""

import datetime as dt

TIMEFRAME_STEP: dict[str, dt.timedelta] = {
    "M1": dt.timedelta(minutes=1),
    "M5": dt.timedelta(minutes=5),
    "M15": dt.timedelta(minutes=15),
    "M30": dt.timedelta(minutes=30),
    "H1": dt.timedelta(hours=1),
    "H4": dt.timedelta(hours=4),
    "D1": dt.timedelta(days=1),
    "W1": dt.timedelta(weeks=1),
}


def step(timeframe: str) -> dt.timedelta:
    """The interval between two consecutive bars of `timeframe`."""
    try:
        return TIMEFRAME_STEP[timeframe]
    except KeyError:
        legal = ", ".join(TIMEFRAME_STEP)
        raise ValueError(f"unknown timeframe {timeframe!r}; expected one of {legal}") from None
