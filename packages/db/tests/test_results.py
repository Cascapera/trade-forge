"""The engine→ORM mapper, checked without a database.

`to_rows` is pure translation, so these tests are too: build the engine's dataclasses by
hand, map them, and assert every field lands in the right column with the right sign, enum
and precision. The one thing only a real Postgres can prove — that the rows satisfy the
table's CHECK constraints — is `test_results_integration.py`.
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from tradeforge_db.models import ExitReason, Trade
from tradeforge_db.results import to_rows
from tradeforge_engine.domain import ClosedTrade, EquityPoint, Side
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
    )


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


def test_context_stringifies_decimals_and_keeps_warming_up_nones() -> None:
    """A JSON number is a float — the exact decimals would be lost on the round trip, so they
    are stored as strings. A `None` (an indicator still warming up) is a fact, not a zero."""
    row = map_one(a_trade(context={"ema": Decimal("1.23456789"), "rsi": None}))
    assert row.context == {"ema": "1.23456789", "rsi": None}


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
