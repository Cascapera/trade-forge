"""How much a run reads before its window (ADR-0030) — the arithmetic, no database."""

import datetime as dt
from decimal import Decimal

from tradeforge_api.warm_window import STRUCTURE_SPAN, WarmUp, warm_start, warmup_for
from tradeforge_engine.domain import Candle

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)


def _days(count: int, first: dt.datetime = START) -> list[Candle]:
    price = Decimal("1.1")
    return [
        Candle(
            time=first + index * DAY, open=price, high=price, low=price, close=price, tick_volume=1
        )
        for index in range(count)
    ]


class TestWarmupFor:
    def test_four_times_the_longest_period_the_document_reads(self) -> None:
        document = {
            "setup": {"type": "mme9_breakout", "params": {"period": 9, "long_average_period": 200}}
        }

        assert warmup_for(document) == WarmUp(bars=800, span=None)

    def test_periods_anywhere_in_the_document_count_a_macd_s_too(self) -> None:
        document = {
            "indicators": [
                {"type": "ema", "params": {"period": 20}},
                {"type": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
            ]
        }

        assert warmup_for(document).bars == 4 * 26

    def test_a_structure_setup_warms_a_calendar_year_and_its_periods_too(self) -> None:
        plain = {"setup": {"type": "structure_choch", "params": {"htf": "H4"}}}
        averaged = {"setup": {"type": "structure_continuation", "params": {"x_period": 50}}}

        assert warmup_for(plain) == WarmUp(bars=0, span=STRUCTURE_SPAN)
        assert warmup_for(averaged) == WarmUp(bars=200, span=STRUCTURE_SPAN)

    def test_a_switched_off_average_and_flags_are_not_periods(self) -> None:
        document = {
            "setup": {
                "type": "mme9_breakout",
                "params": {"period": 9, "long_average_period": None, "volume_filter": True},
            }
        }

        assert warmup_for(document) == WarmUp(bars=36, span=None)


class TestWarmStart:
    def test_the_bars_asked_before_the_window(self) -> None:
        candles = _days(100)
        opens = START + 60 * DAY

        assert warm_start(candles, WarmUp(bars=10, span=None), opens) == 50

    def test_a_calendar_span_reaches_back_by_time(self) -> None:
        candles = _days(500)
        opens = START + 400 * DAY

        assert warm_start(candles, WarmUp(bars=10, span=STRUCTURE_SPAN), opens) == 400 - 365

    def test_whichever_reaches_further_back(self) -> None:
        candles = _days(500)
        opens = START + 400 * DAY

        assert warm_start(candles, WarmUp(bars=380, span=STRUCTURE_SPAN), opens) == 20

    def test_a_history_shorter_than_asked_warms_on_what_there_is(self) -> None:
        candles = _days(100)

        assert warm_start(candles, WarmUp(bars=800, span=None), START + 30 * DAY) == 0
        assert warm_start(candles, WarmUp(bars=0, span=STRUCTURE_SPAN), START + 30 * DAY) == 0

    def test_nothing_asked_nothing_read(self) -> None:
        candles = _days(100)

        assert warm_start(candles, WarmUp(bars=0, span=None), START + 30 * DAY) == 30
