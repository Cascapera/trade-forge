"""Swap: what holding a position overnight earns or costs — a rate per lot per night, signed.

A forex position still open at the daily rollover is charged (or paid) the interest difference
between the two currencies. His account (24/09): GBPUSD pays -5 USD per standard lot per night,
long and short alike, and a rate can as well be a **credit** — which is why this is not a
`CostModel`: a cost is a magnitude the engine refuses to see negative (`Fill`), and a swap is a
signed amount that lands on the trade's result.

**When the nights fall.** The rollover is 17:00 New York time, the forex convention: it is the
midnight of the brokers' servers that run on UTC+2 in winter and UTC+3 in summer — his among them
— and anchoring on New York carries the daylight-saving shift without a parameter anyone has to
keep in step. The market is shut from Friday's close to Sunday's open, so the weekend is not
rolled; the broker charges it in advance instead, on the Wednesday night, three times over.
A week held whole therefore pays seven nights in five rollovers:

    Mon 1 · Tue 1 · Wed 3 · Thu 1 · Fri 1      (Sat and Sun: none)

⚠️ **Charged when the trade closes, not night by night.** The trade's result and the account's
final balance are exact; what the equity curve does not show is the swap accruing while the
position is still open. A backtest's drawdown can therefore read slightly better than the account's
did, by at most the swap of the open trade.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from tradeforge_engine.domain import ZERO, Money, Side, Volume

ROLLOVER_ZONE: Final = ZoneInfo("America/New_York")
ROLLOVER_HOUR: Final = 17

NIGHTS_ROLLED: Final[dict[int, int]] = {0: 1, 1: 1, 2: 3, 3: 1, 4: 1, 5: 0, 6: 0}
"""How many nights each New York weekday's 17:00 rollover charges (Monday is 0). Wednesday carries
the weekend; Saturday and Sunday roll nothing — the market is shut."""


def nights_held(entry_time: dt.datetime, exit_time: dt.datetime) -> int:
    """The nights charged to a position opened at `entry_time` and closed at `exit_time`.

    A rollover counts when the position was open across it: opened before the instant and closed
    after it. One opened or closed exactly at a rollover was not held through it.
    """
    if exit_time <= entry_time:
        return 0
    start = entry_time.astimezone(ROLLOVER_ZONE)
    end = exit_time.astimezone(ROLLOVER_ZONE)
    nights = 0
    day = start.date()
    while day <= end.date():
        rollover = dt.datetime(day.year, day.month, day.day, ROLLOVER_HOUR, tzinfo=ROLLOVER_ZONE)
        if start < rollover < end:
            nights += NIGHTS_ROLLED[day.weekday()]
        day += dt.timedelta(days=1)
    return nights


@dataclass(frozen=True, slots=True)
class SwapRates:
    """The broker's swap, in account currency per standard lot per night, one rate per side.

    Signed as the broker quotes it: negative is charged to the account, positive is paid into it.
    """

    long_per_lot: Money = ZERO
    short_per_lot: Money = ZERO

    def on(
        self, side: Side, volume: Volume, entry_time: dt.datetime, exit_time: dt.datetime
    ) -> Money:
        """What this position's swap came to — signed, like the rates."""
        rate = self.long_per_lot if side is Side.LONG else self.short_per_lot
        if rate == ZERO:
            return ZERO
        return rate * volume * nights_held(entry_time, exit_time)


__all__ = ["NIGHTS_ROLLED", "ROLLOVER_HOUR", "ROLLOVER_ZONE", "SwapRates", "nights_held"]
