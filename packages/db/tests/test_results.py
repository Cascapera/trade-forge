"""The engine→ORM mapper, checked without a database.

`to_rows` is pure translation, so these tests are too: build the engine's dataclasses by
hand, map them, and assert every field lands in the right column with the right sign, enum
and precision. The one thing only a real Postgres can prove — that the rows satisfy the
table's CHECK constraints — is `test_results_integration.py`.
"""

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_db.base import MONEY
from tradeforge_db.models import ExitReason, Trade
from tradeforge_db.results import (
    _MONEY_QUANTUM,
    _trade_row,
    close_trade_values,
    closed_trade_row,
    open_trade_row,
    to_rows,
)
from tradeforge_engine.domain import (
    Candle,
    ClosedTrade,
    EntrySnapshot,
    EquityPoint,
    Position,
    Side,
    SnapshotPoint,
    SnapshotRegion,
    SnapshotSeries,
)
from tradeforge_engine.metrics import BacktestMetrics

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
BACKTEST_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
INSTRUMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def a_trade(  # noqa: PLR0913 — keyword-only; a trade simply has this many facts
    *,
    reason: str = "exit.condition",
    side: Side = Side.LONG,
    net: str = "100",
    stop: str | None = None,
    take_profit: str | None = None,
    r_multiple: str | None = None,
    context: dict[str, Decimal | None] | None = None,
    snapshot: EntrySnapshot | None = None,
) -> ClosedTrade:
    net_pnl = Decimal(net)
    return ClosedTrade(
        symbol="EURUSD",
        side=side,
        volume=Decimal("1"),
        entry_time=START,
        entry_price=Decimal("1.10000"),
        exit_time=START + HOUR,
        exit_price=Decimal("1.10100"),
        gross_pnl=net_pnl,
        costs=Decimal("0"),
        net_pnl=net_pnl,
        reason=reason,
        stop_loss=Decimal(stop) if stop is not None else None,
        take_profit=Decimal(take_profit) if take_profit is not None else None,
        r_multiple=Decimal(r_multiple) if r_multiple is not None else None,
        context=context,
        snapshot=snapshot,
    )


def a_candle(index: int, *, close: str) -> Candle:
    price = Decimal(close)
    return Candle(
        time=START + index * HOUR,
        open=price,
        high=price + Decimal("0.00050"),
        low=price - Decimal("0.00050"),
        close=price,
    )


def a_snapshot(
    *,
    regions: tuple[SnapshotRegion, ...] = (),
    series: tuple[SnapshotSeries, ...] = (),
) -> EntrySnapshot:
    """Two bars: a decision and the fill that followed it."""
    bars = (a_candle(0, close="1.10000"), a_candle(1, close="1.10100"))
    return EntrySnapshot(bars=bars, decided_at=bars[0].time, regions=regions, series=series)


def a_metrics(**overrides: object) -> BacktestMetrics:
    values: dict[str, object] = {
        "net_profit": Decimal("100"),
        "gross_profit": Decimal("200"),
        "gross_loss": Decimal("-100"),
        "total_trades": 2,
        "long_trades": 1,
        "short_trades": 1,
        "win_rate": Decimal("0.5"),
        "payoff": Decimal("2"),
        "profit_factor": Decimal("2"),
        "expectancy": Decimal("50"),
        "max_drawdown_abs": Decimal("100"),
        "max_drawdown_pct": Decimal("0.0098"),
        "max_drawdown_duration": dt.timedelta(days=3, hours=4),
        "sharpe": Decimal("0.5"),
        "sortino": Decimal("0.7"),
        "cagr": None,
        "avg_trade_duration": dt.timedelta(minutes=30),
        "equity_curve": (
            EquityPoint(time=START, equity=Decimal("10000")),
            EquityPoint(time=START + HOUR, equity=Decimal("10100.50")),
        ),
    }
    values.update(overrides)
    return BacktestMetrics(**values)  # type: ignore[arg-type]


def map_one(trade: ClosedTrade) -> Trade:
    _, trades = to_rows(
        trades=[trade],
        metrics=a_metrics(),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    return trades[0]


# --------------------------------------------------------------------------- #
# Trade rows                                                                    #
# --------------------------------------------------------------------------- #


def test_trade_row_carries_every_field_across() -> None:
    row = map_one(
        a_trade(
            side=Side.SHORT,
            net="250",
            stop="1.10500",
            take_profit="1.09000",
            r_multiple="2.5",
            context={"fast": Decimal("1.09925"), "slow": Decimal("1.09900")},
        )
    )
    assert row.backtest_id == BACKTEST_ID
    assert row.instrument_id == INSTRUMENT_ID
    assert row.direction is Side.SHORT
    assert row.entry_time == START
    assert row.entry_price == Decimal("1.10000")
    assert row.volume == Decimal("1")
    assert row.exit_time == START + HOUR
    assert row.exit_price == Decimal("1.10100")
    assert row.stop_loss == Decimal("1.10500")
    assert row.take_profit == Decimal("1.09000")
    assert row.gross_pnl == Decimal("250")
    assert row.costs == Decimal("0")
    assert row.net_pnl == Decimal("250")
    assert row.r_multiple == Decimal("2.5")
    assert row.context == {"fast": "1.09925", "slow": "1.09900"}


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("sl", ExitReason.STOP_LOSS),
        ("tp", ExitReason.TAKE_PROFIT),
        ("exit.condition", ExitReason.CONDITION),
    ],
)
def test_exit_reason_is_mapped_from_the_engine_string(reason: str, expected: ExitReason) -> None:
    assert map_one(a_trade(reason=reason)).exit_reason is expected


def test_an_unmapped_exit_reason_raises_rather_than_defaulting() -> None:
    """A reason the engine never emits is a bug, not a row to persist under a guessed label —
    the same doctrine as `build_indicator` refusing an unknown indicator type."""
    with pytest.raises(ValueError, match="no exit reason mapped for 'kill'"):
        map_one(a_trade(reason="kill"))


def test_a_stopless_trade_maps_its_nullable_fields_to_none() -> None:
    row = map_one(a_trade())  # no stop, no target, no r_multiple, no context
    assert row.stop_loss is None
    assert row.take_profit is None
    assert row.r_multiple is None
    assert row.context == {}
    assert row.snapshot == {}  # NOT NULL: "nothing was recorded", never a NULL to interpret


def test_context_stringifies_decimals_and_keeps_warming_up_nones() -> None:
    """A JSON number is a float — the exact decimals would be lost on the round trip, so they
    are stored as strings. A `None` (an indicator still warming up) is a fact, not a zero."""
    row = map_one(a_trade(context={"ema": Decimal("1.23456789"), "rsi": None}))
    assert row.context == {"ema": "1.23456789", "rsi": None}


def test_the_snapshot_becomes_bars_and_regions_with_string_prices() -> None:
    """Same precision rule as `context`, applied to a series instead of a map: a JSON number is
    a float, and a chart drawn from floats disagrees in the last place with the trade printed
    next to it. Times are ISO with the offset, because a naive one is not an instant."""
    row = map_one(a_trade(snapshot=a_snapshot()))

    assert row.snapshot["decided_at"] == "2024-01-01T00:00:00+00:00"
    assert row.snapshot["filled_at"] == "2024-01-01T01:00:00+00:00"
    assert row.snapshot["bars"] == [
        {
            "time": "2024-01-01T00:00:00+00:00",
            "open": "1.10000",
            "high": "1.10050",
            "low": "1.09950",
            "close": "1.10000",
        },
        {
            "time": "2024-01-01T01:00:00+00:00",
            "open": "1.10100",
            "high": "1.10150",
            "low": "1.10050",
            "close": "1.10100",
        },
    ]
    assert row.snapshot["regions"] == []
    assert row.snapshot["series"] == []


def test_a_region_keeps_its_own_left_edge_in_time() -> None:
    """The rectangle is drawn from the candle that formed the zone, which is older than the
    entry and often older than the window's first bar. Flattening it to two prices would make
    the chart guess that edge, and it would guess the window's left edge — a lie about the age
    of the zone."""
    zone = SnapshotRegion(
        label="zone",
        top=Decimal("1.10200"),
        bottom=Decimal("1.10000"),
        from_time=START - 5 * HOUR,
    )
    row = map_one(a_trade(snapshot=a_snapshot(regions=(zone,))))

    assert row.snapshot["regions"] == [
        {
            "label": "zone",
            "top": "1.10200",
            "bottom": "1.10000",
            "from_time": "2023-12-31T19:00:00+00:00",
        }
    ]


def test_a_series_becomes_time_value_pairs_with_the_value_as_a_string() -> None:
    """The curve, stored the way it is drawn: one pair per bar, in order.

    Pairs and not objects — a fifty-point curve would repeat two keys fifty times for nothing,
    and unlike a bar (five fields, read by a human debugging) a point is only ever consumed by
    the drawing code. The value keeps the exact `Decimal` as a string, like every other price
    here: a curve rounded to a float would not pass through the level in `context`, and that
    equality is the only thing making the curve trustworthy.
    """
    # Three bars with the decision in the middle, so a two-point curve ends *on* the decision
    # while the bars still run one further to the fill — the shape every real snapshot has.
    # The shared `a_snapshot` decides on its first bar, which a two-point curve would overrun,
    # and `EntrySnapshot` refuses that: an indicator read after the decision is lookahead.
    bars = (
        a_candle(0, close="1.10000"),
        a_candle(1, close="1.10100"),
        a_candle(2, close="1.10200"),
    )
    curve = SnapshotSeries(
        label="average",
        points=(
            SnapshotPoint(time=START, value=Decimal("1.0999999999")),
            SnapshotPoint(time=START + HOUR, value=Decimal("1.1000000001")),
        ),
    )
    snapshot = EntrySnapshot(bars=bars, decided_at=bars[1].time, series=(curve,))
    row = map_one(a_trade(snapshot=snapshot))

    assert row.snapshot["series"] == [
        {
            "label": "average",
            "points": [
                ["2024-01-01T00:00:00+00:00", "1.0999999999"],
                ["2024-01-01T01:00:00+00:00", "1.1000000001"],
            ],
        }
    ]


def test_the_snapshot_is_json_safe_all_the_way_down() -> None:
    """JSONB takes no `Decimal` and no `datetime`. A value that slipped through as either would
    only fail at the driver, inside the worker's transaction — after the run had succeeded."""
    row = map_one(
        a_trade(
            snapshot=a_snapshot(
                regions=(
                    SnapshotRegion(
                        label="zone",
                        top=Decimal("1.10200"),
                        bottom=Decimal("1.10000"),
                        from_time=START,
                    ),
                )
            )
        )
    )
    assert json.loads(json.dumps(row.snapshot)) == row.snapshot


def test_one_trade_row_is_built_per_closed_trade() -> None:
    _, rows = to_rows(
        trades=[a_trade(net="100"), a_trade(net="-50"), a_trade(net="20")],
        metrics=a_metrics(),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    assert [row.net_pnl for row in rows] == [Decimal("100"), Decimal("-50"), Decimal("20")]


# --------------------------------------------------------------------------- #
# Metrics row                                                                   #
# --------------------------------------------------------------------------- #


def test_metrics_row_carries_every_field_across() -> None:
    row, _ = to_rows(
        trades=[], metrics=a_metrics(), backtest_id=BACKTEST_ID, instrument_id=INSTRUMENT_ID
    )
    assert row.backtest_id == BACKTEST_ID
    assert row.net_profit == Decimal("100")
    assert row.gross_profit == Decimal("200")
    assert row.gross_loss == Decimal("-100")
    assert row.total_trades == 2
    assert row.long_trades == 1
    assert row.short_trades == 1
    assert row.win_rate == Decimal("0.5")
    assert row.payoff == Decimal("2")
    assert row.profit_factor == Decimal("2")
    assert row.expectancy == Decimal("50")
    assert row.max_drawdown_abs == Decimal("100")
    assert row.max_drawdown_pct == Decimal("0.0098")
    assert row.sharpe == Decimal("0.5")
    assert row.sortino == Decimal("0.7")
    assert row.avg_trade_duration == dt.timedelta(minutes=30)


def test_drawdown_duration_is_stored_as_whole_days() -> None:
    """The column is granular to the day (PR-101). Three days and four hours is three days;
    a sub-day drawdown is zero."""
    row, _ = to_rows(
        trades=[],
        metrics=a_metrics(max_drawdown_duration=dt.timedelta(days=3, hours=4)),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    assert row.max_dd_duration_days == 3

    intraday, _ = to_rows(
        trades=[],
        metrics=a_metrics(max_drawdown_duration=dt.timedelta(hours=10)),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    assert intraday.max_dd_duration_days == 0


def test_undefined_metrics_stay_none_never_zero() -> None:
    """A run with no losses has no profit factor, a short run no Sharpe. Those are `None` in
    the engine and must stay `None` in the row — a 0 would rank a broken run as a real one."""
    row, _ = to_rows(
        trades=[],
        metrics=a_metrics(
            payoff=None,
            profit_factor=None,
            sharpe=None,
            sortino=None,
            cagr=None,
            expectancy=None,
            avg_trade_duration=None,
        ),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    assert row.payoff is None
    assert row.profit_factor is None
    assert row.sharpe is None
    assert row.sortino is None
    assert row.cagr is None
    assert row.expectancy is None
    assert row.avg_trade_duration is None


def test_equity_curve_becomes_json_with_iso_time_and_string_equity() -> None:
    row, _ = to_rows(
        trades=[], metrics=a_metrics(), backtest_id=BACKTEST_ID, instrument_id=INSTRUMENT_ID
    )
    assert row.equity_curve == [
        {"time": "2024-01-01T00:00:00+00:00", "equity": "10000"},
        {"time": "2024-01-01T01:00:00+00:00", "equity": "10100.50"},
    ]


# --------------------------------------------------------------------------- #
# Rounding to the column's scale                                                #
# --------------------------------------------------------------------------- #
#
# Every number below came out of the engine driven end to end (the MA-cross golden dataset
# under a `SpreadCostModel`, swept over tick values), not from arithmetic invented to fail.
# The engine's money carries ten to twelve decimal places; the columns hold eight.
#
# These assertions compare the *text*, not the value. `Decimal` compares numerically, so
# `Decimal("90.98800909880") == Decimal("90.98800909")` is False but the identity
# `net == gross_profit + gross_loss` holds exactly in unbounded `Decimal` — which means an
# assertion written that way passes against the defect it is supposed to forbid. Only the
# scale of the number on the wire separates the two.


def test_money_quantum_matches_the_column_it_rounds_to() -> None:
    """The constant and the column have to agree. If `MONEY` is ever widened and this is left
    behind, the rounding silently goes back to being Postgres's, one place too early."""
    scale = MONEY.scale
    assert scale is not None, "MONEY must declare a scale; without one the column is not exact"
    assert _MONEY_QUANTUM.as_tuple().exponent == -scale


def test_metrics_money_is_rounded_to_the_column_scale() -> None:
    row, _ = to_rows(
        trades=[],
        metrics=a_metrics(
            gross_profit=Decimal("191.44801914480"),
            gross_loss=Decimal("-100.46001004600"),
            net_profit=Decimal("90.98800909880"),
        ),
        backtest_id=BACKTEST_ID,
        instrument_id=INSTRUMENT_ID,
    )
    assert str(row.gross_profit) == "191.44801914"
    assert str(row.gross_loss) == "-100.46001005"
    # Not "90.98800910", which is what rounding the engine's own total gives. The total is
    # derived from the two rounded halves, so `net_profit_balances` holds on the stored row.
    assert str(row.net_profit) == "90.98800909"


def test_trade_money_is_rounded_to_the_column_scale() -> None:
    row = map_one(
        ClosedTrade(
            symbol="EURUSD",
            side=Side.LONG,
            volume=Decimal("1"),
            entry_time=START,
            entry_price=Decimal("1.10000"),
            exit_time=START + HOUR,
            exit_price=Decimal("1.10100"),
            gross_pnl=Decimal("199.0882702401"),
            costs=Decimal("0.423062575000"),
            net_pnl=Decimal("198.665207665100"),
            reason="tp",
        )
    )
    assert str(row.gross_pnl) == "199.08827024"
    assert str(row.costs) == "0.42306258"
    # Not "198.66520767" — same reason as above, for `net_pnl_balances`.
    assert str(row.net_pnl) == "198.66520766"


# --------------------------------------------------------------------------- #
# A session that has not finished (rev_0012)                                    #
# --------------------------------------------------------------------------- #

SESSION_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def a_position(**overrides: object) -> Position:
    values: dict[str, Any] = {
        "symbol": "EURUSD",
        "side": Side.LONG,
        "volume": Decimal("0.5"),
        "entry_price": Decimal("1.10000"),
        "entry_time": START,
        "entry_costs": Decimal("5"),
        "stop_loss": Decimal("1.09500"),
        "initial_stop_loss": Decimal("1.09000"),
        "take_profit": Decimal("1.12000"),
    }
    values.update(overrides)
    return Position(**values)


def test_an_open_trade_carries_no_exit_at_all() -> None:
    """The four exit columns arrive together or not at all. A row with, say, an `exit_time` and
    no `net_pnl` is refused by the database — so an open position has to be all four absent,
    not "the ones we happen to know"."""
    row = open_trade_row(a_position(), SESSION_ID, INSTRUMENT_ID)

    assert (row.exit_time, row.exit_price, row.exit_reason, row.net_pnl) == (None, None, None, None)
    assert row.live_session_id == SESSION_ID
    assert row.backtest_id is None


def test_an_open_trade_records_the_stop_it_was_sized_against() -> None:
    """⚠️ The separating test: the position's two stops differ, and only one is correct.

    `Position.stop_loss` is where the stop is *now* — a trailing strategy moves it every bar
    (ADR-0018). `initial_stop_loss` is what the lot was sized against, and it is the
    denominator `r_multiple` divides by. Recording the moved one would make the stored risk
    drift with the trailing, and every R afterwards would be measured against a number the
    trade never risked. A fixture whose two stops were equal could not tell these apart.
    """
    row = open_trade_row(
        a_position(stop_loss=Decimal("1.09500"), initial_stop_loss=Decimal("1.09000")),
        SESSION_ID,
        INSTRUMENT_ID,
    )

    assert row.stop_loss == Decimal("1.09000")


def test_an_open_trade_keeps_the_entry_context_exactly() -> None:
    """Same treatment as a backtest's: stringified so no `Decimal` becomes a float, and `None`
    surviving as `None` because a warming-up indicator read nothing, which is not a zero."""
    row = open_trade_row(
        a_position(context={"ema9": Decimal("1.10500"), "adx": None}), SESSION_ID, INSTRUMENT_ID
    )

    assert row.context == {"ema9": "1.10500", "adx": None}


def test_closing_sets_the_exit_and_leaves_the_entry_alone() -> None:
    """The UPDATE payload. Anything settled at the fill must be absent from it — a close that
    could rewrite the entry price, the volume or the stop is a close that rewrites history, and
    the R multiple would then be reported against a stop the row no longer shows."""
    values = close_trade_values(a_trade(reason="tp"))

    assert set(values) == {
        "exit_time",
        "exit_price",
        "exit_reason",
        "take_profit",
        "gross_pnl",
        "costs",
        "net_pnl",
        "r_multiple",
    }
    assert "entry_price" not in values
    assert "volume" not in values
    assert "stop_loss" not in values


def test_a_closed_live_trade_says_what_a_backtest_trade_would() -> None:
    """The acceptance of PR-302-B, stated as an equality rather than as a description.

    "Trades persisted identically in format to a backtest's" is not something a docstring can
    promise. The same `ClosedTrade` goes through both translators here, and every column they
    both write has to match — otherwise a paper session and the backtest it is supposed to
    reproduce would disagree in the database while agreeing in the engine.
    """
    closed = a_trade(reason="sl")

    backtest_row = map_one(closed)
    live_values = close_trade_values(closed)

    for column, value in live_values.items():
        assert getattr(backtest_row, column) == value, f"{column} disagrees with the backtest"


def test_the_net_of_a_live_close_balances_the_way_the_check_demands() -> None:
    """`net_pnl = gross_pnl - costs`, derived from the two **rounded** legs rather than rounded
    itself. Rounding the net independently is how the CHECK gets violated by a value that looks
    right to the cent — the bug PR-235 chased into the database's edge."""
    # ⚠️ These digits are chosen, not decorative. Measured: with `gross=100.005, costs=3.335`
    # both readings print 96.67000000 — so a translator that simply copied the engine's net
    # would pass, and `Decimal` equality could not see the difference anyway. The pair below is
    # one where the two roundings genuinely disagree at the MONEY quantum (1E-8): rounding the
    # legs gives ...02, rounding the net gives ...01. That is the whole gap the CHECK lives in.
    #
    # Built by hand rather than through `a_trade`, which pins costs at zero — and a trade with
    # no costs cannot separate "derived" from "copied" at all.
    awkward = ClosedTrade(
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("1"),
        entry_time=START,
        entry_price=Decimal("1.10000"),
        exit_time=START + HOUR,
        exit_price=Decimal("1.10100"),
        gross_pnl=Decimal("100.000000015"),
        costs=Decimal("3.000000005"),
        net_pnl=Decimal("100.000000015") - Decimal("3.000000005"),
        reason="tp",
    )
    values = close_trade_values(awkward)

    assert values["net_pnl"] == Decimal("97.00000002"), "the net was copied, not derived"

    assert values["net_pnl"] == values["gross_pnl"] - values["costs"]


def test_a_live_round_trip_that_never_had_an_open_row_maps_to_a_finished_one() -> None:
    """A trade that opened and closed inside one bar. The ordinary live path writes twice — at
    the fill and at the close — but a round trip with no bar in between never had a moment
    anybody could have observed it open, so there is nothing for the first write to say."""
    session_id = uuid.uuid4()
    instrument_id = uuid.uuid4()

    row = closed_trade_row(a_trade(net="120"), session_id, instrument_id)

    assert row.live_session_id == session_id
    assert row.backtest_id is None, "a live trade must not claim a backtest parent"
    assert row.instrument_id == instrument_id
    assert row.exit_time is not None
    assert row.net_pnl == Decimal("120")


def test_a_trade_row_refuses_to_belong_to_both_parents_or_neither() -> None:
    """⚠️ `trade_has_one_parent` on the table would catch this, but only at flush — by then the
    caller is a bar into a live session and the error names a constraint rather than the call
    that built the row. Both halves are here because a guard written as `or` instead of the
    equality of two `is None` checks passes one of them and not the other."""
    with pytest.raises(ValueError, match="exactly one"):
        _trade_row(a_trade(), uuid.uuid4())

    with pytest.raises(ValueError, match="exactly one"):
        _trade_row(
            a_trade(),
            uuid.uuid4(),
            backtest_id=uuid.uuid4(),
            live_session_id=uuid.uuid4(),
        )
