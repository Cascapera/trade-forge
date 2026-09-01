"""The one decision in `/live-sessions` that does not need a database: which day is "today".

Everything else in that router is a query, and a unit test of a query is a test of a double. This
is a pure function, and it is the one place a wrong answer would be *plausible* — the panel would
simply total a different day from the cap that halts trading, and both numbers would look fine.
"""

import datetime as dt

from tradeforge_api.routers.live_sessions import _start_of_day

# 00:30 on the 1st, UTC — which is 21:30 on the 31st where Guilherme is (UTC-3), and 03:30 on
# the 1st where the broker is (UTC+3). Three clocks, three different "todays", one right answer.
MIDNIGHT_ISH = dt.datetime(2026, 9, 1, 0, 30, tzinfo=dt.UTC)


def test_today_starts_at_midnight_utc() -> None:
    assert _start_of_day(MIDNIGHT_ISH) == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def test_an_instant_given_in_another_zone_still_lands_on_the_utc_day() -> None:
    """⚠️ **The test that makes the `astimezone` observable at all.**

    Passed a `now` that is already UTC, an implementation with the conversion and one without are
    the same function — every test above would pass either. So the guard reads as dead code and
    the next person deletes it, and the day it matters (something hands this a local clock) it
    fails by quietly totalling the wrong day.

    Same instant as `MIDNIGHT_ISH`, spelled in his own zone: 21:30 on the 31st, UTC-3. The UTC day
    it belongs to is the **1st**, not the 31st that the wall clock reads.
    """
    his_clock = MIDNIGHT_ISH.astimezone(dt.timezone(-dt.timedelta(hours=3)))
    assert his_clock.day == 31, "the fixture only means something if the wall clocks disagree"

    assert _start_of_day(his_clock) == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def test_the_brokers_clock_lands_on_the_same_day() -> None:
    """The other direction, because an off-by-one zone conversion is wrong on only one side.

    17:00 UTC on the 1st is 20:00 on the 1st at the broker (UTC+3) — same day either way — but
    22:00 UTC is *01:00 on the 2nd* there. A conversion applied backwards would put that trade on
    tomorrow's total.
    """
    late = dt.datetime(2026, 9, 1, 22, 0, tzinfo=dt.UTC)
    broker_clock = late.astimezone(dt.timezone(dt.timedelta(hours=3)))
    assert broker_clock.day == 2, "the fixture only means something if the wall clocks disagree"

    assert _start_of_day(broker_clock) == dt.datetime(2026, 9, 1, tzinfo=dt.UTC)


def test_midnight_itself_starts_the_day_it_opens() -> None:
    """A trade closed at exactly 00:00:00 is today's, not yesterday's — the boundary is inclusive
    on the near side, which is what the `>=` in the query relies on."""
    midnight = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)

    assert _start_of_day(midnight) == midnight
