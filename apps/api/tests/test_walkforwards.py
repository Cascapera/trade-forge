"""`/walkforwards` with no database: the cut, the two reads, and the shape on the wire.

The fold arithmetic lives in `test_walkforward.py` and the wiring against Postgres lives in
`test_walkforwards_integration.py`. What is left is the router's own seam — where it turns a
dataset into folds, and where it turns rows into a response — and it is exactly the part where a
wrong answer arrives without anything crashing: a window cut from the dates somebody *asked for*
instead of the candles that exist, a run that has not finished reported as a zero, or a metric
read off the wrong column.

Every test here builds ORM rows in memory. The two that need candles write a real Parquet
dataset into `tmp_path`, because reading the instants off disk is the behaviour under test —
`_folds_for` exists to distrust the requested window, and a fake list of timestamps would let it
be right by construction.
"""

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.routers.walkforwards import (
    _chosen_strategies,
    _fold_out,
    _folds_for,
    _in_sample_runs,
    _return_of,
    _test_runs,
)
from tradeforge_api.schemas import CreateWalkForwardRequest
from tradeforge_api.walkforward import Fold, Window
from tradeforge_api.worker import _METRIC_OF, _candidates
from tradeforge_collector import write_candles
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    SelectionMetric,
    Strategy,
    Study,
    WalkForwardFold,
)
from tradeforge_engine.domain import Candle
from tradeforge_engine.testing import HOUR, START, bar

SYMBOL = "EURUSD"
TIMEFRAME = "H1"
GRID: dict[str, list[Any]] = {"indicators.1.params.period": [5, 9]}

DOCUMENT: dict[str, Any] = {
    "indicators": [
        {"id": "fast", "params": {"period": 2}},
        {"id": "slow", "params": {"period": 9}},
    ]
}
"""A stored point's document, holding the slow average this fold's grid varies."""


def _metrics(**overrides: object) -> BacktestMetrics:
    """A finished run's metrics, with every nullable column filled unless a test empties it.

    Every value below is **distinct**, and that is the point rather than tidiness: a mapping
    that read `net_profit` for every metric would pass against a fixture where two of them
    happened to be equal, and three of these four columns are the kind of ratio that lands on
    the same number by coincidence.
    """
    defaults: dict[str, object] = {
        "net_profit": Decimal("1200"),
        "gross_profit": Decimal("2000"),
        "gross_loss": Decimal("-800"),
        "total_trades": 40,
        "long_trades": 25,
        "short_trades": 15,
        "win_rate": Decimal("0.55"),
        "payoff": Decimal("1.4"),
        "profit_factor": Decimal("2.5"),
        "expectancy": Decimal("30"),
        "max_drawdown_abs": Decimal("300"),
        "max_drawdown_pct": Decimal("0.03"),
        "max_dd_duration_days": 4,
        "sharpe": Decimal("1.9"),
        "sortino": Decimal("2.7"),
        "cagr": Decimal("0.18"),
        "equity_curve": [],
    }
    return BacktestMetrics(**(defaults | overrides))


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (SelectionMetric.NET_PROFIT, Decimal("1200")),
        (SelectionMetric.PROFIT_FACTOR, Decimal("2.5")),
        (SelectionMetric.SHARPE, Decimal("1.9")),
        (SelectionMetric.EXPECTANCY, Decimal("30")),
    ],
)
def test_each_metric_reads_its_own_column(metric: SelectionMetric, expected: Decimal) -> None:
    """The ranking a fold was asked for is the ranking it gets.

    Worth its own test because the failure is silent in the worst way: a walk-forward asked to
    select on Sharpe but selecting on net profit still runs, still fills every fold, and still
    reports an out-of-sample number — one that answers a question nobody asked.
    """
    assert _METRIC_OF[metric](_metrics()) == expected


@pytest.mark.parametrize(
    "metric",
    [SelectionMetric.PROFIT_FACTOR, SelectionMetric.SHARPE, SelectionMetric.EXPECTANCY],
)
def test_an_undefined_metric_stays_undefined(metric: SelectionMetric) -> None:
    """Three of the four columns are nullable, and null has to survive the read.

    Sharpe over zero trades is undefined, not flat. If it arrived here as a zero, `choose` could
    never tell "no score" from "scored nothing" — and in a losing window a zero beats every
    point that traded and lost.
    """
    assert _METRIC_OF[metric](_metrics(**{metric.value: None})) is None


def test_every_selection_metric_is_readable() -> None:
    """The mapping covers the enum, checked over the enum rather than over a list.

    A metric added to `SelectionMetric` without a line in `_METRIC_OF` would pass the request
    validator, reach the worker, and die with a `KeyError` inside a job — minutes into an
    experiment, with the failure recorded against the walk-forward rather than the code.
    """
    assert set(_METRIC_OF) == set(SelectionMetric)


def test_a_run_that_has_not_finished_returns_null_and_never_zero() -> None:
    """A fold's missing number is an absence of measurement, not a measurement of nothing.

    Zero would place the fold in the median, count it against `folds_profitable`, and drag the
    compounded figure toward nothing — all while the run it came from was still queued.
    """
    assert _return_of(None, Decimal("10000")) is None
    assert _return_of(Backtest(), Decimal("10000")) is None


def test_a_return_is_a_fraction_of_the_capital_the_run_started_with() -> None:
    """Fractions, not currency, because that is what makes folds comparable and compoundable."""
    run = Backtest()
    run.metrics = _metrics(net_profit=Decimal("1200"))

    assert _return_of(run, Decimal("10000")) == Decimal("0.12")


# --------------------------------------------------------------------------- #
# Cutting the folds: read from the dataset, never from the dates requested       #
# --------------------------------------------------------------------------- #


def _flat(count: int) -> list[Candle]:
    """`count` hourly bars starting at `START`. The prices are never read — only the instants.

    `_folds_for` counts candles and reports where they fall; nothing in it opens a price. A
    market shape here would suggest this test says something about trading, and it does not.
    """
    return [bar(index, open_="100", close="100") for index in range(count)]


def _settings(tmp_path: Path) -> Settings:
    """Settings whose only meaningful field is the Parquet root.

    The password is required by `PostgresSettings` and deliberately not read: nothing in this
    file opens a connection, and a settings object that refused to exist without one would push
    a test that needs no database into the integration suite.
    """
    return Settings(postgres_password="no connection is opened in this file", parquet_root=tmp_path)


def _study(**overrides: Any) -> Study:
    """A study asking for a window wider on one side than any dataset a test writes."""
    defaults: dict[str, Any] = {
        "timeframe": TIMEFRAME,
        # Opens thirty days before any dataset a test writes, and closes on the 90th bar of one —
        # so a cut taken from these dates and a read that ignored them are both visibly wrong.
        "date_from": START - dt.timedelta(days=30),
        "date_to": START + 89 * HOUR,
        "grid": GRID,
    }
    return Study(**(defaults | overrides))


def _request(**overrides: Any) -> CreateWalkForwardRequest:
    body: dict[str, Any] = {"study_id": uuid.uuid4(), "folds": 2, "train_multiple": 1}
    return CreateWalkForwardRequest(**(body | overrides))


def test_the_folds_are_cut_from_the_candles_on_disk_not_from_the_dates_requested(
    tmp_path: Path,
) -> None:
    """**The dataset decides the boundaries, and the requested window still clips it.**

    A hundred bars are on disk and the study asks for a window that opens thirty days before
    the first of them and closes on the ninetieth — the ordinary case, since somebody types
    round dates and the backfill covers what it covers. Two different wrong answers are ruled
    out here, and they fail in opposite directions:

    * cutting the *request* into three even parts puts the first fold entirely before the first
      candle, reported as an even split with `train_bars` claiming evidence that never existed;
    * reading the dataset without the window reaches bar 99, so the last fold is scored on ten
      candles the study it descends from never covered.

    The two assertions on the ends are what separates them: thirty bars a window, opening on
    the first candle written and closing on the last one the study asked for.
    """
    write_candles(tmp_path, SYMBOL, TIMEFRAME, _flat(100))

    folds = _folds_for(_settings(tmp_path), _study(), SYMBOL, _request(folds=2, train_multiple=1))

    # 90 candles inside the window, over `train_multiple + folds` = 3 parts, no remainder.
    assert [fold.train.bars for fold in folds] == [30, 30]
    assert [fold.test.bars for fold in folds] == [30, 30]
    assert folds[0].train.start == START
    assert folds[-1].test.end == START + 89 * HOUR


def test_a_study_whose_window_holds_no_candles_is_refused_by_name(tmp_path: Path) -> None:
    """Empty is refused here rather than left to the splitter, and the message is why.

    An empty list reaches `split` as "zero candles cut into two folds", which is true and
    useless: the reader's actual problem is that this symbol and timeframe were never
    backfilled over this window. Refusing at the read means the reply names the dataset.
    """
    with pytest.raises(HTTPException) as raised:
        _folds_for(_settings(tmp_path), _study(), SYMBOL, _request())

    assert raised.value.status_code == 422
    assert "has no candles" in str(raised.value.detail)


def test_a_split_that_cannot_be_cut_is_the_callers_error_and_not_a_crash(tmp_path: Path) -> None:
    """More folds than the history supports is a 422 carrying the splitter's own sentence.

    `WalkForwardError` escaping unhandled would be a 500, which says the server broke when what
    happened is that ninety candles cannot answer this question — and the caller can fix it by
    asking for fewer folds, but only if told so.
    """
    write_candles(tmp_path, SYMBOL, TIMEFRAME, _flat(90))

    with pytest.raises(HTTPException) as raised:
        _folds_for(_settings(tmp_path), _study(), SYMBOL, _request(folds=4, train_multiple=3))

    assert raised.value.status_code == 422
    assert "ask for fewer folds" in str(raised.value.detail)


# --------------------------------------------------------------------------- #
# One fold on the wire                                                          #
# --------------------------------------------------------------------------- #


def _window(first: int, last: int, bars: int) -> Window:
    return Window(start=START + first * HOUR, end=START + last * HOUR, bars=bars)


def _row(**overrides: Any) -> WalkForwardFold:
    defaults: dict[str, Any] = {
        "index": 0,
        "study_id": uuid.uuid4(),
        "test_from": START + 30 * HOUR,
        "test_to": START + 59 * HOUR,
        "train_bars": 30,
        "test_bars": 30,
    }
    return WalkForwardFold(**(defaults | overrides))


def test_a_fold_just_created_takes_its_training_window_from_the_cut() -> None:
    """On the creation path the fold's own study has not been read back, so the cut answers.

    Nothing has been decided yet either, and every one of those fields is null rather than a
    placeholder — a fresh walk-forward that reported `0.0` out of sample would be indis-
    tinguishable on screen from one whose folds all broke even.
    """
    fold = Fold(index=0, train=_window(0, 29, 30), test=_window(30, 59, 30))

    out = _fold_out(_row(), fold, chosen=None, in_sample=None, test=None, grid=GRID)

    assert (out.train_from, out.train_to) == (START, START + 29 * HOUR)
    assert out.chosen_label is None
    assert out.in_sample_return is None
    assert out.out_of_sample_return is None
    assert out.test_status is None
    assert out.test_trades is None


def test_a_decided_fold_is_named_by_the_values_it_chose_not_by_the_strategys_name() -> None:
    """The read path: the window comes from the fold's study, and the label from the document.

    Naming the point `period=9` is what lets a reader match a fold's choice to a cell of the
    heatmap `GET /studies/{id}` draws. The stored strategy's *name* carries the base strategy in
    front of the values (`MME9 [period=9]`), so serving that here would leave the two sides of
    the comparison this feature exists for unable to be lined up.
    """
    row = _row(study=_study(date_from=START, date_to=START + 29 * HOUR))
    in_sample = Backtest()
    in_sample.metrics = _metrics(net_profit=Decimal("1200"))
    test = Backtest(status=BacktestStatus.DONE)
    test.metrics = _metrics(net_profit=Decimal("-500"), total_trades=7)

    out = _fold_out(
        row,
        None,
        chosen=Strategy(definition=DOCUMENT),
        in_sample=in_sample,
        test=test,
        grid=GRID,
        capital=Decimal("10000"),
    )

    assert (out.train_from, out.train_to) == (START, START + 29 * HOUR)
    assert out.chosen_label == "period=9"
    assert out.in_sample_return == Decimal("0.12")
    assert out.out_of_sample_return == Decimal("-0.05")
    assert out.test_status == "done"
    assert out.test_trades == 7


class _Documents:
    """Stands in for the session in `_candidates`, whose only use of one is to fetch documents.

    It answers with the same strategies whatever it is asked, so nothing below proves the query
    filters correctly — that is the integration suite's job, against Postgres. What it makes
    testable without a container is the half a database could never catch anyway: which column
    the score is read from, and that a run with no metrics ranks as unscored rather than as a
    zero.
    """

    def __init__(self, strategies: list[Strategy]) -> None:
        self._strategies = strategies

    def scalars(self, statement: Any) -> list[Strategy]:
        """The statement is accepted and ignored — see the class docstring."""
        return self._strategies


def _point(period: int) -> Strategy:
    document = {"indicators": [{"id": "fast"}, {"id": "slow", "params": {"period": period}}]}
    return Strategy(id=uuid.uuid4(), definition=document)


def test_a_training_run_without_metrics_is_unscored_rather_than_scored_zero() -> None:
    """The rule the whole experiment rests on, at the moment a run becomes rankable.

    A fold's grid is ranked by one number, and three different things arrive as no number: a run
    that failed, a run still queued, and a metric undefined for the run (Sharpe over zero
    trades). Read as zero, any of them **wins a losing window** — every point that traded lost
    money, the broken one "returned nothing", and the fold reports the failure as its choice.
    The out-of-sample run that follows would then be launched on a point nobody selected.
    """
    winner, failed = _point(5), _point(9)
    ran = Backtest(strategy_id=winner.id)
    ran.metrics = _metrics(net_profit=Decimal("-800"), total_trades=12)
    never_ran = Backtest(strategy_id=failed.id)
    fold = _row(study=_study(grid=GRID))

    ranked = _candidates(
        _Documents([winner, failed]),  # type: ignore[arg-type]
        fold,
        [ran, never_ran],
        SelectionMetric.NET_PROFIT,
    )

    by_label = {candidate.label: candidate for candidate in ranked}
    assert by_label["period=5"].score == Decimal("-800")
    assert by_label["period=5"].trades == 12
    assert by_label["period=9"].score is None
    assert by_label["period=9"].trades == 0
    assert ranked[by_label["period=9"]] == failed.id


def test_a_candidate_is_placed_on_the_grid_by_the_document_its_run_executed() -> None:
    """Coordinates come from the document, and they are the tie-break `choose` breaks ties with.

    Deriving them from the stored name would work until a value contained a comma; deriving them
    from the order rows came back in would make "the best point" depend on Postgres. Both are
    the same defect the study read was fixed for, and a tie broken differently in the two places
    would let one endpoint's winner disagree with the other's over the same runs.
    """
    slow, fast = _point(9), _point(5)  # deliberately not in the grid's order
    runs = [Backtest(strategy_id=slow.id), Backtest(strategy_id=fast.id)]

    ranked = _candidates(
        _Documents([slow, fast]),  # type: ignore[arg-type]
        _row(study=_study(grid=GRID)),
        runs,
        SelectionMetric.SHARPE,
    )

    assert {candidate.label: candidate.coordinates for candidate in ranked} == {
        "period=5": (0,),
        "period=9": (1,),
    }


def test_the_selection_metric_reaches_the_candidate_it_ranks() -> None:
    """Asked for Sharpe, the candidate carries Sharpe — not the net profit sitting beside it.

    `_METRIC_OF` is proven column by column above; what this adds is that the metric the request
    named is the one `_candidates` hands `choose`. A walk-forward that selected on net profit
    while reporting itself as a Sharpe experiment runs to completion and looks right.
    """
    point = _point(9)
    run = Backtest(strategy_id=point.id)
    run.metrics = _metrics(sharpe=Decimal("1.9"), net_profit=Decimal("1200"))

    ranked = _candidates(
        _Documents([point]),  # type: ignore[arg-type]
        _row(study=_study(grid=GRID)),
        [run],
        SelectionMetric.SHARPE,
    )

    assert [candidate.score for candidate in ranked] == [Decimal("1.9")]


def test_an_undecided_walk_forward_asks_the_database_for_nothing() -> None:
    """A walk-forward whose folds chose nothing does not query, and the session proves it.

    The `Session` below is bound to no engine, so any query at all raises rather than returning
    an empty result — which is the whole assertion. `IN ()` over an empty set is legal SQL and
    costs a round trip per fold to learn what the caller already knew, and it is the shape that
    quietly turns a six-fold read into three pointless queries the moment a run fails.
    """
    unbound = Session()
    rows = [_row(index=index) for index in range(3)]

    assert _chosen_strategies(unbound, rows) == {}
    assert _in_sample_runs(unbound, rows) == {}
    assert _test_runs(unbound, rows) == {}
