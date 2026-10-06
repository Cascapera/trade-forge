"""The variables of an entry (ADR-0031, rule 1): read at the decision bar, never after it."""

import datetime as dt
import math
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradeforge_engine.domain import Candle
from tradeforge_ml.feature_export import features_table, read_market
from tradeforge_ml.features import (
    FEATURE_NAMES,
    Market,
    ema,
    features_at,
    rsi,
    true_range,
    wilder,
)

H1 = dt.timedelta(hours=1)
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def bars(
    closes: list[float],
    *,
    spread: float = 0.5,
    start: dt.datetime = T0,
    step: dt.timedelta = H1,
    points: int = 0,
) -> list[Candle]:
    """A bar per close, its open the previous close and its range `spread` either side; `points`
    the broker's spread each bar records (0: none recorded)."""
    out = []
    previous = closes[0]
    for n, close in enumerate(closes):
        top = max(previous, close) + spread
        bottom = min(previous, close) - spread
        out.append(
            Candle(
                time=start + n * step,
                open=Decimal(str(previous)),
                high=Decimal(str(top)),
                low=Decimal(str(bottom)),
                close=Decimal(str(close)),
                tick_volume=100 + n,
                spread=points,
            )
        )
        previous = close
    return out


def walk(count: int, seed: int = 7) -> list[float]:
    rng = random.Random(seed)  # noqa: S311 — a test's market, not a secret
    level, out = 100.0, []
    for _ in range(count):
        level += rng.uniform(-1, 1)
        out.append(round(level, 4))
    return out


def same(left: dict[str, float], right: dict[str, float]) -> bool:
    return all(
        (math.isnan(left[k]) and math.isnan(right[k])) or left[k] == right[k] for k in FEATURE_NAMES
    )


class TestTheIndicators:
    def test_the_ema_worked_by_hand(self) -> None:
        # alpha = 2 / 4 = 0.5: 10, 10 + 0.5 * (20 - 10) = 15, 15 + 0.5 * (30 - 15) = 22.5
        assert list(ema(np.array([10.0, 20.0, 30.0]), 3)) == [10.0, 15.0, 22.5]

    def test_wilders_smoothing_worked_by_hand(self) -> None:
        # weight 1/2: 4, 4 + (8 - 4) / 2 = 6, 6 + (2 - 6) / 2 = 4
        assert list(wilder(np.array([4.0, 8.0, 2.0]), 2)) == [4.0, 6.0, 4.0]

    def test_the_true_range_reaches_to_the_previous_close_over_a_gap(self) -> None:
        high = np.array([11.0, 15.0])
        low = np.array([9.0, 14.0])
        close = np.array([10.0, 14.5])
        # The second bar gapped up from 10: its range runs from 10 to 15, not 14 to 15.
        assert list(true_range(high, low, close)) == [2.0, 5.0]

    def test_rsi_is_fifty_where_nothing_moved_and_bounded(self) -> None:
        assert rsi(np.array([5.0, 5.0, 5.0]))[-1] == 50.0
        rising = rsi(np.array([float(n) for n in range(1, 40)]))
        assert rising[-1] == 100.0
        assert ((rising >= 0) & (rising <= 100)).all()


class TestTheDecisionBar:
    market = Market.of(bars(walk(10)), H1)

    def test_an_entry_inside_a_bar_reads_the_bar_before(self) -> None:
        # Bar 4 opens at T0+4h and is still open at T0+4h30: the last one closed is bar 3.
        assert self.market.decision_bar(T0 + 4 * H1 + dt.timedelta(minutes=30)) == 3

    def test_an_entry_at_a_bars_open_reads_the_bar_that_just_closed(self) -> None:
        assert self.market.decision_bar(T0 + 4 * H1) == 3

    def test_an_entry_before_any_bar_closed_has_none(self) -> None:
        assert self.market.decision_bar(T0 + dt.timedelta(minutes=30)) is None
        values = features_at(
            self.market, entry_time=T0, side="long", entry_price=100.0, stop_loss=99.0
        )
        assert all(math.isnan(values[name]) for name in FEATURE_NAMES)


class TestNoLookahead:
    """ADR-0031 rule 1, proved by construction: whatever the bars after the decision bar say,
    every variable is the same."""

    @pytest.mark.parametrize("side", ["long", "short"])
    @pytest.mark.parametrize("decision", [210, 260, 349])
    def test_changing_every_later_bar_changes_nothing(self, side: str, decision: int) -> None:
        rng = random.Random(decision)  # noqa: S311 — a test's market, not a secret
        # Spreads that vary, so one more bar would move their median.
        candles = [replace(bar, spread=rng.randint(1, 50)) for bar in bars(walk(400))]
        entry = T0 + (decision + 1) * H1 + dt.timedelta(minutes=20)
        altered = [
            bar
            if n <= decision
            else replace(
                bar,
                open=Decimal(str(rng.uniform(1, 500))),
                high=Decimal(str(rng.uniform(500, 900))),
                low=Decimal(str(rng.uniform(0.1, 1))),
                close=Decimal(str(rng.uniform(1, 500))),
                tick_volume=rng.randint(1, 10_000),
                spread=rng.randint(1, 500),
            )
            for n, bar in enumerate(candles)
        ]

        before = features_at(
            Market.of(candles, H1), entry_time=entry, side=side, entry_price=101.0, stop_loss=99.5
        )
        after = features_at(
            Market.of(altered, H1), entry_time=entry, side=side, entry_price=101.0, stop_loss=99.5
        )

        assert Market.of(candles, H1).decision_bar(entry) == decision
        assert same(before, after)

    def test_the_decision_bar_itself_does_count(self) -> None:
        """The other half of the proof: the test above would pass on variables that read
        nothing at all."""
        candles = bars(walk(300))
        decision = 250
        entry = T0 + (decision + 1) * H1
        moved = [
            replace(bar, close=bar.close + 5, high=bar.high + 5) if n == decision else bar
            for n, bar in enumerate(candles)
        ]

        before = features_at(
            Market.of(candles, H1), entry_time=entry, side="long", entry_price=101.0, stop_loss=99.0
        )
        after = features_at(
            Market.of(moved, H1), entry_time=entry, side="long", entry_price=101.0, stop_loss=99.0
        )

        assert not same(before, after)


class TestOrientedBySide:
    def test_a_direction_reads_the_same_for_a_long_and_a_mirrored_short(self) -> None:
        market = Market.of(bars(walk(300)), H1)
        entry = T0 + 280 * H1
        long = features_at(market, entry_time=entry, side="long", entry_price=100, stop_loss=99)
        short = features_at(market, entry_time=entry, side="short", entry_price=100, stop_loss=101)

        for name in ("ema20_dist_atr", "ema200_dist_atr", "ret5_atr", "ema_stack"):
            assert short[name] == pytest.approx(-long[name]), name
        assert short["range_pos20"] == pytest.approx(1 - long["range_pos20"])
        assert short["rsi14"] == pytest.approx(100 - long["rsi14"])
        # Neither side changes what has no direction.
        for name in ("atr_pct", "atr_ratio", "bar_range_atr", "stop_atr", "hour_utc"):
            assert short[name] == long[name], name

    def test_the_stop_is_measured_in_atrs_and_the_clock_is_the_decision_bars_close(self) -> None:
        market = Market.of(bars([100.0] * 30, spread=0.5), H1)
        entry = T0 + 25 * H1 + dt.timedelta(minutes=10)
        values = features_at(market, entry_time=entry, side="long", entry_price=100, stop_loss=98)

        # A flat market with bars one point tall: the ATR is 1, so a stop 2 points away is 2 ATR.
        assert values["stop_atr"] == pytest.approx(2.0)
        assert values["hour_utc"] == 1.0  # bar 24 opens at 00:00 on 2 January and closes at 01:00
        assert values["weekday"] == 1.0  # Tuesday


class TestTooLittleHistory:
    def test_a_variable_that_looks_back_further_than_there_are_bars_is_empty(self) -> None:
        market = Market.of(bars(walk(60)), H1)
        values = features_at(
            market, entry_time=T0 + 55 * H1, side="long", entry_price=100, stop_loss=99
        )

        assert math.isnan(values["ema200_dist_atr"])
        assert math.isnan(values["ema_stack"])
        assert math.isnan(values["range_pos100"])
        assert math.isnan(values["atr_ratio"])
        assert not math.isnan(values["ema50_dist_atr"])
        assert values["bars_before"] == 55.0


class TestTheTable:
    def test_one_row_per_event_in_its_order_and_a_chart_without_bars_said(self) -> None:
        candles = bars(walk(300))
        events = pa.table(
            {
                "entry_id": ["e1", "e1", "e2"],
                "symbol": ["EURUSD", "NOPE", "EURUSD"],
                "timeframe": ["H1", "H1", "H1"],
                "side": ["long", "short", "short"],
                "entry_time": pa.array(
                    [T0 + 250 * H1, T0 + 250 * H1, T0 + 260 * H1], type=pa.timestamp("us", tz="UTC")
                ),
                "entry_price": [100.0, 100.0, 101.0],
                "stop_loss": [99.0, 101.0, 102.0],
            }
        )

        market = Market.of(candles, H1)
        table, report = features_table(
            events, lambda symbol, _timeframe: market if symbol == "EURUSD" else None
        )

        assert table.num_rows == 3
        assert table.column("symbol").to_pylist() == ["EURUSD", "NOPE", "EURUSD"]
        assert table.column("stop_atr").to_pylist()[1] is None
        assert table.column("stop_atr").to_pylist()[2] is not None
        assert (report.markets, report.without_bars, report.missing_markets) == (1, 1, ["NOPE H1"])


def test_a_chart_is_read_from_the_collectors_layout_in_time_order(tmp_path: Path) -> None:
    """The files the collector writes (`symbol=/timeframe=/year=`), years listed out of order."""
    chart = tmp_path / "symbol=EURUSD" / "timeframe=H1"
    candles = bars(walk(30))
    for year, part in (("2025", candles[20:]), ("2024", candles[:20])):
        (chart / f"year={year}").mkdir(parents=True)
        pq.write_table(
            pa.table(
                {
                    "time": pa.array([bar.time for bar in part], type=pa.timestamp("us", tz="UTC")),
                    "open": [float(bar.open) for bar in part],
                    "high": [float(bar.high) for bar in part],
                    "low": [float(bar.low) for bar in part],
                    "close": [float(bar.close) for bar in part],
                    "tick_volume": [bar.tick_volume for bar in part],
                    "spread": pa.array([n % 7 for n in range(len(part))], type=pa.int32()),
                }
            ),
            chart / f"year={year}" / "part.parquet",
        )

    read = read_market(tmp_path, "EURUSD", "H1", point=0.0001)

    assert read is not None
    assert list(read.close) == [float(bar.close) for bar in candles]
    # 2024's 20 bars then 2025's 10, each year's zeros read as none recorded.
    expected = [float(n % 7) or math.nan for n in [*range(20), *range(10)]]
    assert np.array_equal(read.spread, np.array(expected), equal_nan=True)
    assert read.point == 0.0001
    assert read.decision_bar(T0 + 10 * H1) == 9
    assert read_market(tmp_path, "GBPUSD", "H1") is None


M15 = dt.timedelta(minutes=15)


def _at(market: Market, entry: dt.datetime, side: str = "long") -> dict[str, float]:
    stop = 99.0 if side == "long" else 101.0
    return features_at(market, entry_time=entry, side=side, entry_price=100.0, stop_loss=stop)


class TestSpread:
    """Against its own recent past and against the stop, never in points: in this broker's history
    the points move in steps by year, so a model would read the year off them."""

    def test_the_spread_is_read_against_its_median_and_in_price_against_the_stop(self) -> None:
        candles = bars([100.0] * 150, points=5)
        candles[140] = replace(candles[140], spread=15)  # the decision bar: three times the usual
        market = Market.of(candles, H1, point=0.01)

        values = _at(market, T0 + 141 * H1)

        assert values["spread_rel"] == pytest.approx(3.0)
        assert values["spread_stop"] == pytest.approx(15 * 0.01 / 1.0)  # stop 1.0 away

    def test_a_bar_with_no_spread_recorded_is_empty_not_free(self) -> None:
        market = Market.of(bars([100.0] * 150, points=0), H1, point=0.01)

        values = _at(market, T0 + 141 * H1)

        assert math.isnan(values["spread_rel"])
        assert math.isnan(values["spread_stop"])

    def test_with_fewer_than_half_the_bars_recorded_there_is_no_typical_spread(self) -> None:
        candles = [
            replace(bar, spread=5 if n >= 100 else 0) for n, bar in enumerate(bars([100.0] * 150))
        ]  # 41 of the last 100 recorded
        market = Market.of(candles, H1, point=0.01)

        values = _at(market, T0 + 141 * H1)

        assert math.isnan(values["spread_rel"])
        assert values["spread_stop"] == pytest.approx(0.05)

    def test_without_the_instruments_point_there_is_no_spread_in_price(self) -> None:
        market = Market.of(bars([100.0] * 150, points=5), H1)

        values = _at(market, T0 + 141 * H1)

        assert values["spread_rel"] == pytest.approx(1.0)
        assert math.isnan(values["spread_stop"])


def _flags(values: dict[str, float]) -> dict[str, float]:
    return {name: values[f"session_{name}"] for name in ("asia", "london", "ny", "ny_cash")}


def _decided_at(moment: dt.datetime, step: dt.timedelta = M15) -> dict[str, float]:
    """The variables of an entry whose decision bar closes at `moment`."""
    market = Market.of(bars(walk(400), start=moment - 300 * step, step=step), step)
    return _at(market, moment)


class TestSessions:
    """In each city's clock, so the same UTC hour is in New York's session in July and not in
    January."""

    def test_new_york_follows_its_own_summer_time(self) -> None:
        january = _decided_at(dt.datetime(2024, 1, 10, 12, 30, tzinfo=dt.UTC))  # 07:30 EST
        july = _decided_at(dt.datetime(2024, 7, 10, 12, 30, tzinfo=dt.UTC))  # 08:30 EDT

        assert _flags(january) == {"asia": 0.0, "london": 1.0, "ny": 0.0, "ny_cash": 0.0}
        assert _flags(july) == {"asia": 0.0, "london": 1.0, "ny": 1.0, "ny_cash": 0.0}

    def test_a_session_is_open_at_its_opening_and_closed_at_its_closing(self) -> None:
        cash_opens = _decided_at(dt.datetime(2024, 1, 10, 14, 30, tzinfo=dt.UTC))  # 09:30 EST
        cash_closes = _decided_at(dt.datetime(2024, 1, 10, 21, 0, tzinfo=dt.UTC))  # 16:00 EST

        assert cash_opens["session_ny_cash"] == 1.0
        assert cash_closes["session_ny_cash"] == 0.0

    def test_no_session_opens_on_a_weekend_in_its_own_city(self) -> None:
        tokyo_monday = _decided_at(dt.datetime(2024, 1, 15, 1, 0, tzinfo=dt.UTC))  # 10:00 JST
        tokyo_sunday = _decided_at(dt.datetime(2024, 1, 14, 1, 0, tzinfo=dt.UTC))  # 10:00 JST
        saturday = _decided_at(dt.datetime(2024, 1, 13, 14, 0, tzinfo=dt.UTC))

        assert _flags(tokyo_monday) == {"asia": 1.0, "london": 0.0, "ny": 0.0, "ny_cash": 0.0}
        assert set(_flags(tokyo_sunday).values()) == {0.0}
        assert set(_flags(saturday).values()) == {0.0}
        assert math.isnan(saturday["session_minutes"])


class TestSessionMove:
    """The market inside the session that opened last, from its first whole bar to the decision."""

    def test_the_move_is_read_from_the_last_opened_session_in_atrs_and_by_side(self) -> None:
        # 10:00 EST on a Wednesday: London, New York and its cash hours are open; the cash hours
        # opened last (14:30 UTC), two M15 bars ago.
        decided = dt.datetime(2024, 1, 10, 15, 0, tzinfo=dt.UTC)
        closes = [100.0] * 298 + [101.0, 103.0]
        market = Market.of(bars(closes, start=decided - 300 * M15, step=M15), M15)
        first = len(closes) - 2  # opens 14:30
        atr = float(market.atr14[-1])

        long, short = _at(market, decided), _at(market, decided, side="short")

        assert long["session_minutes"] == 30.0
        assert long["session_ret_atr"] == pytest.approx((103.0 - float(market.open[first])) / atr)
        assert short["session_ret_atr"] == pytest.approx(-long["session_ret_atr"])
        top = float(market.high[first:].max())
        bottom = float(market.low[first:].min())
        assert long["session_range_atr"] == pytest.approx((top - bottom) / atr)
        assert long["session_pos"] == pytest.approx((103.0 - bottom) / (top - bottom))
        assert short["session_pos"] == pytest.approx(1 - long["session_pos"])

    def test_at_the_opening_itself_nothing_has_moved_yet(self) -> None:
        values = _decided_at(dt.datetime(2024, 1, 10, 14, 30, tzinfo=dt.UTC))  # cash opens now

        assert values["session_minutes"] == 0.0
        assert math.isnan(values["session_ret_atr"])
        assert math.isnan(values["session_pos"])

    def test_above_h1_a_bar_straddles_the_opening_so_only_the_flags_are_read(self) -> None:
        values = _decided_at(dt.datetime(2024, 1, 10, 16, 0, tzinfo=dt.UTC), dt.timedelta(hours=4))

        assert values["session_ny"] == 1.0
        for name in ("session_ret_atr", "session_range_atr", "session_pos", "session_minutes"):
            assert math.isnan(values[name]), name
