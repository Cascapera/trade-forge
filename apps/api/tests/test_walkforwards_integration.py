"""`/walkforwards` over a real Postgres, a real Parquet dataset and the real engine.

`test_walkforward.py` proves the fold arithmetic with no I/O, and `test_walkforwards.py` proves
the two column reads with no database. What is left needs all three, and it is the part that
would be wrong without anything crashing: that a fold's training runs really are confined to
its training window, that the point chosen in-sample is the point that runs out of sample, and
that the folds share one set of strategies rather than writing a new one per fold.

Run locally with `docker compose up -d`, then:

    POSTGRES_DB=tradeforge_test uv run pytest -m integration

⚠️ The variable is not optional. Without it the integration suite truncates whatever database
the environment points at, which on a developer machine is the real one.
"""

import asyncio
import datetime as dt
import random
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import RUN_WALK_FORWARD
from tradeforge_api.worker import process_walk_forward
from tradeforge_collector import write_candles
from tradeforge_db.models import (
    Backtest,
    BacktestStatus,
    Instrument,
    Strategy,
    Study,
    WalkForward,
)
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.testing import bar

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
SYMBOL = "EURUSD"
CAPITAL = "10000"

# 120 bars, two folds, training as long as testing: three parts of 40. Fold 0 trains on bars
# 0-39 and is tested on 40-79; fold 1 trains on 40-79 and is tested on 80-119. Small on purpose
# — this file runs the engine 2 x 2 + 2 times, and every extra point is another real backtest.
BARS = 120
FOLDS = 2
TRAIN_MULTIPLE = 1
WINDOW = 40


class _CapturingQueue:
    """Stands in for the arq pool: records what would have been enqueued instead of sending it."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args))


class _SilentRedis:
    """Stands in for Redis while the worker runs inline. Progress is not this file's subject."""

    async def publish(self, channel: str, message: str) -> None:
        return None


def _seed_instrument(session: Session) -> None:
    session.add(
        Instrument(
            symbol=SYMBOL,
            name=f"{SYMBOL} for the walk-forward tests",
            asset_class=AssetClass.FOREX,
            currency_base="EUR",
            currency_quote="USD",
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            contract_size=Decimal("100000"),
            digits=5,
        )
    )
    session.commit()


def _zigzag() -> list[Candle]:
    """A market that keeps crossing, so every window contains trades.

    ⚠️ **A window with no trades would make this whole file vacuous.** A fold whose grid never
    traded chooses nothing, reports null on both sides, and every assertion about *which* point
    won would pass against an implementation that chose at random — because it never chose at
    all. The saw shape is chosen so a short moving average crosses a longer one repeatedly, in
    every 40-bar stretch rather than only in the first.

    The drift is deliberate too: a perfectly symmetric saw returns to the same price every
    cycle, and then every parameter set returns exactly zero and the ranking has nothing to
    rank.

    ⚠️ **And the drift steepens from one window to the next, which is a second thing entirely.**
    A saw with one constant drift makes every 40-bar stretch an exact copy of its neighbours:
    forex profit is linear in the price *difference*, so all three windows return the same
    number to the cent. Every fold then reports the same figure on both sides of its line, the
    verdict's degradation is exactly zero — and a report that read its in-sample number off the
    wrong row would still say zero. Measured over these bars: flat drift gives every window
    +608.00 for `period=5`; stepped, the windows separate.
    """
    levels: list[str] = []
    price = Decimal("1.10000")
    for index in range(BARS + 1):
        # Six bars up, four bars down, and a net drift upward that steepens once per window.
        rising = Decimal("0.00080") + Decimal("0.00010") * (index // WINDOW)
        swing = rising if index % 10 < 6 else Decimal("-0.00100")
        price += swing
        levels.append(f"{price:.5f}")
    return [bar(i, open_=levels[i], close=levels[i + 1]) for i in range(BARS)]


def _strategy() -> dict[str, Any]:
    """An MA cross, because its one parameter is the one a grid over this method would vary."""
    return {
        "schema_version": "1.0",
        "name": "MA cross under walk-forward",
        "timeframe": "H1",
        "indicators": [
            {"id": "fast", "type": "SMA", "params": {"period": 2}},
            {"id": "slow", "type": "SMA", "params": {"period": 5}},
        ],
        "entry": {
            "long": {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}},
            "short": None,
        },
        "exit": {
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 2, "side": "low"}},
            "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}},
            "conditions": [],
        },
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _study_body(strategy_id: str) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "symbol": SYMBOL,
        "timeframe": "H1",
        "date_from": START.isoformat(),
        "date_to": (START + (BARS + 10) * HOUR).isoformat(),
        "initial_capital": CAPITAL,
        "cost_model": {"type": "none"},
        # Two points, and the axis is the slow average's period so the two really do trade
        # differently over the same candles.
        "grid": {"indicators.1.params.period": [5, 9]},
    }


def _app(settings: Settings, session_factory: Callable[[], Session], tmp_path: Path) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=_CapturingQueue(),
    )


def _launch(client: TestClient, **overrides: Any) -> tuple[str, dict[str, Any]]:
    """Create the strategy, run the study, then ask for the walk-forward of that study."""
    strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
    study = client.post("/studies", json=_study_body(strategy_id))
    assert study.status_code == 202, study.text

    response = client.post(
        "/walkforwards",
        json={
            "study_id": study.json()["id"],
            "folds": FOLDS,
            "train_multiple": TRAIN_MULTIPLE,
            **overrides,
        },
    )
    assert response.status_code == 202, response.text
    body: dict[str, Any] = response.json()
    return study.json()["id"], body


def _drive(session_factory: Callable[[], Session], tmp_path: Path, walk_forward_id: str) -> None:
    """Run the orchestrating job inline, the way arq would have."""
    session = session_factory()
    try:
        asyncio.run(
            process_walk_forward(
                session=session,
                redis=_SilentRedis(),  # type: ignore[arg-type]
                parquet_root=tmp_path,
                walk_forward_id=uuid.UUID(walk_forward_id),
            )
        )
    finally:
        session.close()


@pytest.fixture
def prepared(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> Callable[[], Any]:
    """Seed the instrument and the candles once; hand back a factory for the app."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, SYMBOL, "H1", _zigzag())

    def build() -> Any:
        return _app(settings, session_factory, tmp_path)

    return build


def test_the_folds_are_cut_from_the_candles_and_never_overlap(
    prepared: Callable[[], Any],
) -> None:
    """**The invariant, asserted against a real dataset rather than a list of instants.**

    A training window that ends on the first bar of its own test window makes every number this
    feature reports a little too good, for ever, and nothing fails. Here the boundaries have
    made the round trip through Parquet, Postgres and JSON, which is where an off-by-one that
    the pure tests cannot see would appear — a date serialised to the day, say, would collapse
    two adjacent hourly bars into one boundary.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)

    folds = created["folds"]
    assert len(folds) == FOLDS
    for fold in folds:
        assert fold["train_bars"] == WINDOW
        assert fold["test_bars"] == WINDOW
        assert fold["train_to"] < fold["test_from"]

    # Contiguous: the second fold's test window opens after the first one's closes, with no
    # stretch of market skipped between them.
    assert folds[0]["test_to"] < folds[1]["test_from"]


def test_a_walk_forward_writes_one_study_per_fold_and_shares_their_strategies(
    prepared: Callable[[], Any], session_factory: Callable[[], Session]
) -> None:
    """⚠️ The consequence of a point's document holding no symbol and no dates.

    `period=9` is byte-identical in fold 1 and fold 2, so the second fold reuses the row the
    first one wrote. Without that reuse the second fold would collide on the unique
    `(name, version)` and the whole request would fail — so this is not an optimisation, it is
    the mechanism. Counted rather than described: two folds of a two-point grid is **two**
    generated strategies, not four.
    """
    with TestClient(prepared()) as client:
        study_id, created = _launch(client)

    session = session_factory()
    try:
        # The base strategy plus one per grid point — the study wrote them, the folds reused
        # them, and neither wrote a second copy.
        assert session.scalar(select(func.count()).select_from(Strategy)) == 3
        # The study the walk-forward was built from, plus one per fold.
        assert session.scalar(select(func.count()).select_from(Study)) == 1 + FOLDS
        # The study's own runs, plus one per point per fold. No test runs yet: the winner is
        # not known until the training has run.
        assert session.scalar(select(func.count()).select_from(Backtest)) == 2 + FOLDS * 2

        fold_studies = {uuid.UUID(fold["study_id"]) for fold in created["folds"]}
        assert len(fold_studies) == FOLDS
        assert uuid.UUID(study_id) not in fold_studies
    finally:
        session.close()


def test_only_one_job_is_queued_for_the_whole_experiment(prepared: Callable[[], Any]) -> None:
    """⚠️ The training runs are written `queued` and deliberately never enqueued.

    Queueing them as well would give every row two claimants — the orchestrator and an ordinary
    worker — and the second one to arrive would try to write a duplicate metrics row. One job,
    whatever the size of the grid.
    """
    app = prepared()
    with TestClient(app) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        study = client.post("/studies", json=_study_body(strategy_id))
        # ⚠️ Counted from *here*, not from an empty queue. The study on the previous line is a
        # real study and enqueues its own two points, so a walk-forward that queued nothing at
        # all would leave a queue that still looked busy.
        before = len(app.state.arq_pool.jobs)

        response = client.post(
            "/walkforwards",
            json={"study_id": study.json()["id"], "folds": FOLDS, "train_multiple": TRAIN_MULTIPLE},
        )
        assert response.status_code == 202, response.text
        created = response.json()

    added = app.state.arq_pool.jobs[before:]
    # One job for `FOLDS x 2` training runs: the count is the assertion, since queueing them as
    # well is the mistake that reads as ordinary and gives every row two claimants.
    assert [name for name, _ in added] == [RUN_WALK_FORWARD]
    assert added[0][1] == (created["id"],)
    assert created["runs_queued"] == FOLDS * 2


def test_the_point_chosen_in_sample_is_the_point_that_runs_out_of_sample(
    prepared: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """The whole pipeline: train, choose, test, report — against the real engine.

    The assertions worth their weight are the last two. **The out-of-sample run must execute the
    chosen strategy** — a walk-forward that trained honestly and then tested the *base* strategy
    would produce an entirely plausible report about an experiment nobody ran. And **its window
    must be the test window**, not the training one, which is the same off-by-one as the split
    arriving one layer later.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])

        read = client.get(f"/walkforwards/{created['id']}")
        assert read.status_code == 200, read.text
        body = read.json()

    assert body["status"] == "done"
    assert body["error"] is None

    decided = [fold for fold in body["folds"] if fold["chosen_label"] is not None]
    assert decided, "no fold chose a point; the fixture traded nothing and proves nothing"

    session = session_factory()
    try:
        for fold in decided:
            # ⚠️ **The point that actually wins, not "one of the two".** Measured by driving the
            # engine over these very bars: on fold 0's training window `period=5` makes 608.00
            # against `period=9`'s -294.40, and on fold 1's, 604.80 against 198.00. Accepting
            # either label would pass against a fold that ranked by the wrong column, or that
            # picked whichever run Postgres returned first — which is the failure this whole
            # file exists to catch, since both still produce a complete, plausible report.
            assert fold["chosen_label"] == "period=5"
            assert fold["test_backtest_id"] is not None

            test_run = session.get(Backtest, uuid.UUID(fold["test_backtest_id"]))
            assert test_run is not None
            assert str(test_run.strategy_id) == fold["chosen_strategy_id"]
            # ⚠️ Compared as instants, never as text. The column hands back `+00:00` and the
            # wire carries `Z` for the very same moment, so a string comparison fails on two
            # spellings of one timestamp — and would just as happily pass two different moments
            # that happened to be spelled alike.
            assert test_run.date_from == dt.datetime.fromisoformat(fold["test_from"])
            assert test_run.date_to == dt.datetime.fromisoformat(fold["test_to"])
            # The out-of-sample run belongs to no study, which is what keeps it visible in the
            # run log while the three hundred training runs stay hidden.
            assert test_run.study_id is None
    finally:
        session.close()

    scored = [fold for fold in body["folds"] if fold["out_of_sample_return"] is not None]
    assert len(scored) == body["verdict"]["folds_scored"]


def test_the_verdict_compares_the_two_sides_of_the_line(
    prepared: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """The headline is computed from the folds that landed, and it is a subtraction of medians.

    Asserted here rather than only in the pure tests because the two numbers reach it by
    different routes — the in-sample side through a query joining a fold's study to its chosen
    strategy, the out-of-sample side through the fold's own test run. Reading either from the
    wrong row would still produce a number.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])
        body = client.get(f"/walkforwards/{created['id']}").json()

    verdict = body["verdict"]
    assert verdict["folds_total"] == FOLDS
    assert verdict["folds_decided"] >= 1
    assert 1 <= verdict["distinct_choices"] <= verdict["folds_decided"]

    if verdict["folds_scored"]:
        assert verdict["degradation"] is not None
        assert Decimal(verdict["degradation"]) == Decimal(
            verdict["out_of_sample_median"]
        ) - Decimal(verdict["in_sample_median"])


def test_the_run_log_hides_the_training_runs_and_keeps_the_tested_ones(
    prepared: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """⚠️ Without this the run log is unusable the first time anybody runs a real experiment.

    A grid's runs are hidden by default because this is a research log and one study buries
    every run made by hand. The out-of-sample runs are **not** hidden, and the difference is
    the point: they are the one run per fold that a decision produced, and they are what a
    reader scanning the log is looking for.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])

        visible = client.get("/backtests").json()
        everything = client.get("/backtests", params={"include_generated": True}).json()
        body = client.get(f"/walkforwards/{created['id']}").json()

    tested = {fold["test_backtest_id"] for fold in body["folds"] if fold["test_backtest_id"]}
    shown = {item["id"] for item in visible["items"]}

    assert tested <= shown
    # Everything else in the log belongs to a grid, so the default view is exactly the tested
    # runs — and the flag brings back the study's own runs plus every fold's training runs.
    assert visible["total"] == len(tested)
    assert everything["total"] == visible["total"] + 2 + FOLDS * 2


def test_a_second_run_of_the_same_walk_forward_changes_nothing(
    prepared: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """⚠️ The job is enqueued under the walk-forward's own id, so a retry runs it again.

    Re-executing a finished run would try to write a second metrics row against the same
    primary key, and a completed fold would come back `failed`. The guard is that runs already
    `done` are skipped — asserted by counting rows, because the visible symptom of getting this
    wrong is not an error but a *second* test run per fold.

    The clock is asserted too, and separately: a completed experiment whose `started_at` moves
    because a queue redelivered a message is a row that now describes the redelivery. Nothing
    errors, and the duration on screen is simply wrong from then on.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        before = session.scalar(select(func.count()).select_from(Backtest))
        finished = session.get(WalkForward, uuid.UUID(created["id"]))
        assert finished is not None
        clock = (finished.started_at, finished.finished_at)
    finally:
        session.close()

    _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(Backtest)) == before
        walk_forward = session.get(WalkForward, uuid.UUID(created["id"]))
        assert walk_forward is not None
        assert walk_forward.status.value == "done", walk_forward.error
        assert (walk_forward.started_at, walk_forward.finished_at) == clock
    finally:
        session.close()


def test_an_experiment_that_died_halfway_resumes_instead_of_failing_again(
    prepared: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """⚠️ **The retry path, which is the one this job actually needs, and it was broken.**

    A worker killed mid-experiment leaves the row `failed` with a `finished_at` from the attempt
    that ended. arq re-enqueues under the walk-forward's own id, so the job runs again — and
    `walk_forwards` carries a CHECK that a finish may not precede its start. A retry that kept
    those stamps died on its first commit, and the constraint violation was then recorded as the
    experiment's failure: a message about a timestamp, standing where the reason the experiment
    failed used to be.

    The row is failed by hand here rather than by killing a worker, because the state a crash
    leaves *is* this row, and arranging a real crash inside a fold would test the crash.
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        walk_forward = session.get(WalkForward, uuid.UUID(created["id"]))
        assert walk_forward is not None
        walk_forward.status = BacktestStatus.FAILED
        walk_forward.error = "the worker was killed"
        session.commit()
        before = session.scalar(select(func.count()).select_from(Backtest))
    finally:
        session.close()

    _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        walk_forward = session.get(WalkForward, uuid.UUID(created["id"]))
        assert walk_forward is not None
        assert walk_forward.status.value == "done", walk_forward.error
        # The stale reason is gone, not carried into a run that succeeded. A completed
        # experiment showing why its previous attempt failed is a screen nobody can read.
        assert walk_forward.error is None
        # And no fold was scored twice: the ones already holding a test run are skipped whole.
        assert session.scalar(select(func.count()).select_from(Backtest)) == before
    finally:
        session.close()


def test_a_study_too_short_to_cut_is_refused_before_anything_is_written(
    prepared: Callable[[], Any], session_factory: Callable[[], Session]
) -> None:
    """A refusal, with the numbers in it, and no half-written experiment left behind.

    Twenty folds over 120 candles leaves five bars per test window — a window that cannot hold
    a trade returns zero, and a screen full of zeroes cannot say whether the method declined or
    the window was too small to ask.
    """
    with TestClient(prepared()) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        study_id = client.post("/studies", json=_study_body(strategy_id)).json()["id"]
        response = client.post("/walkforwards", json={"study_id": study_id, "folds": 20})

    assert response.status_code == 422, response.text
    assert "under the" in response.json()["detail"]

    session = session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(WalkForward)) == 0
        # The study's own runs are all that exist: no fold study, no training run.
        assert session.scalar(select(func.count()).select_from(Study)) == 1
    finally:
        session.close()


def test_a_walk_forward_of_a_study_that_does_not_exist_is_a_404(
    prepared: Callable[[], Any],
) -> None:
    with TestClient(prepared()) as client:
        response = client.post("/walkforwards", json={"study_id": str(uuid.uuid4())})

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# The acceptance criterion: a market where the grid over-fits, caught          #
# --------------------------------------------------------------------------- #

# `specs/fase-2.md`, PR-204: "teste com dataset sintético onde estratégia overfit falha no
# out-of-sample". Everything above proves the pipeline is *correct*; this proves it is
# *useful* — that the report says so when a choice was fitted to its own sample.
#
# Three windows of 200 bars, and the middle one behaves differently from its neighbours:
#
#   0-199    calm    half-cycle 10, jitter ±5 ticks    ->  fold 0 trains here
#   200-399  rough   half-cycle 3,  jitter ±20 ticks   ->  fold 0 is tested here, fold 1 trains
#   400-599  calm    half-cycle 10, jitter ±5 ticks    ->  fold 1 is tested here
OVERFIT_WINDOW = 200
OVERFIT_GRID = [3, 5, 8, 13, 21]


def _two_regimes() -> list[Candle]:
    """A market that changes character once, and changes back.

    ⚠️ **The jitter is what makes the over-fitting possible, and it took measuring to find.**
    On a clean saw every period from 3 to 13 catches the same swings and returns the same
    number, so no parameter can be "tuned" to anything — the first version of this fixture
    produced five identical columns. A *little* noise breaks the tie in favour of the **longer**
    average, because the short one is the one that gets sawn by it. That is the whole trick: the
    calm window teaches "longer is better", which is a fact about the noise, not about the
    market.

    Deterministic by seed. A fixture that depended on chance would be a test that sometimes
    proves the feature and sometimes proves nothing.
    """
    # S311: this is a market fixture, not a secret. A seeded generator is exactly what is
    # wanted — the same bars on every run, on every machine.
    noise = random.Random(7)  # noqa: S311
    closes: list[Decimal] = []
    price = Decimal("1.10000")
    for span, half, jitter in (
        (OVERFIT_WINDOW, 10, 5),
        (OVERFIT_WINDOW, 3, 20),
        (OVERFIT_WINDOW, 10, 5),
    ):
        for index in range(span):
            # Symmetric: up as far as it comes down. A drift would make the market profitable
            # on its own, and then every point wins and nothing can degrade.
            rising = (index // half) % 2 == 0
            price += Decimal("0.00100") if rising else Decimal("-0.00100")
            closes.append(price + Decimal(noise.randint(-jitter, jitter)) * Decimal("0.00001"))

    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=previous,
                high=max(previous, close) + Decimal("0.00020"),
                low=min(previous, close) - Decimal("0.00020"),
                close=close,
            )
        )
        previous = close
    return candles


@pytest.fixture
def over_fitting(
    session_factory: Callable[[], Session], settings: Settings, tmp_path: Path
) -> Callable[[], Any]:
    """The same app, over the two-regime market. `tmp_path` is per-test, so this dataset and
    the zigzag above never share a directory."""
    seeding = session_factory()
    _seed_instrument(seeding)
    seeding.close()
    write_candles(tmp_path, SYMBOL, "H1", _two_regimes())

    def build() -> Any:
        return _app(settings, session_factory, tmp_path)

    return build


def test_a_choice_fitted_to_its_own_window_is_reported_as_the_loss_it_becomes(
    over_fitting: Callable[[], Any],
    session_factory: Callable[[], Session],
    tmp_path: Path,
) -> None:
    """**The acceptance criterion of PR-204, and the only test here that exercises its purpose.**

    Every other test in this file proves the pipeline is correct: the windows do not overlap,
    the chosen point is the one that runs, the folds share their strategies. None of them proves
    the thing the feature exists for — that when a grid fits its own sample, the report says so.
    A walk-forward that silently reported healthy numbers over an over-fitted choice would pass
    all of them.

    Measured over these bars before any of it was asserted:

    | slow | f0 train (calm) | f0 test (rough) | f1 train (rough) | f1 test (calm) |
    |------|----------------:|----------------:|-----------------:|---------------:|
    | 3    |        1 925,18 |         -391,37 |          -391,37 |       1 930,90 |
    | 13   |    **1 936,00** |    **-1 379,62** |        -1 379,62 |       1 932,80 |

    **Fold 0 is the over-fitting.** The calm window prefers `period=13` — by eleven currency
    units over `period=3`, an edge that is entirely an artefact of the jitter — and that choice
    then loses **1 379**, three and a half times what the point it beat would have lost.

    **Fold 1 is the method working**, and it is here so the test cannot pass on a walk-forward
    that simply reports everything as bad: trained on the rough window it picks `period=3`, and
    that earns **+1 930** on the calm one.

    The assertions are on the *shape* rather than on the cents: which point each fold chose, the
    signs on either side of its line, and a negative degradation. The figures above document what
    was measured; pinning them to the cent would make an engine change look like a broken test.
    """
    with TestClient(over_fitting()) as client:
        strategy_id = client.post("/strategies", json=_strategy()).json()["id"]
        study = client.post(
            "/studies",
            json={
                "strategy_id": strategy_id,
                "symbol": SYMBOL,
                "timeframe": "H1",
                "date_from": START.isoformat(),
                "date_to": (START + (3 * OVERFIT_WINDOW + 10) * HOUR).isoformat(),
                "initial_capital": CAPITAL,
                "cost_model": {"type": "none"},
                "grid": {"indicators.1.params.period": OVERFIT_GRID},
            },
        )
        assert study.status_code == 202, study.text

        created = client.post(
            "/walkforwards",
            json={"study_id": study.json()["id"], "folds": 2, "train_multiple": 1},
        )
        assert created.status_code == 202, created.text
        walk_forward_id = created.json()["id"]

        _drive(session_factory, tmp_path, walk_forward_id)
        body = client.get(f"/walkforwards/{walk_forward_id}").json()

    assert body["status"] == "done", body["error"]
    first, second = body["folds"]

    # The fold that fitted its window: it chose the long average the calm sample rewarded...
    assert first["chosen_label"] == "period=13"
    assert Decimal(first["in_sample_return"]) > 0
    # ...and the window it never saw took the money back.
    assert Decimal(first["out_of_sample_return"]) < 0

    # The fold that did not: a different choice, and it survived out of sample.
    assert second["chosen_label"] == "period=3"
    assert Decimal(second["out_of_sample_return"]) > 0

    verdict = body["verdict"]
    # ⚠️ The two symptoms a reader is meant to see, and the reason both are reported. The folds
    # disagreeing says the grid was reading the sample; the negative degradation says what that
    # cost. Either alone is ambiguous — a stable choice can still degrade, and folds can disagree
    # while every one of them earns.
    assert verdict["distinct_choices"] == 2
    assert Decimal(verdict["degradation"]) < 0
    assert verdict["folds_profitable"] == 1
