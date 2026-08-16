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
from tradeforge_db.models import Backtest, Instrument, Strategy, Study, WalkForward
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
    """
    levels: list[str] = []
    price = Decimal("1.10000")
    for index in range(BARS + 1):
        # Six bars up, four bars down, and a small net drift upward over each cycle.
        swing = Decimal("0.00080") if index % 10 < 6 else Decimal("-0.00100")
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
        _, created = _launch(client)

    assert [name for name, _ in app.state.arq_pool.jobs] == [RUN_WALK_FORWARD]
    assert app.state.arq_pool.jobs[0][1] == (created["id"],)
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
            assert fold["chosen_label"] in ("period=5", "period=9")
            assert fold["test_backtest_id"] is not None

            test_run = session.get(Backtest, uuid.UUID(fold["test_backtest_id"]))
            assert test_run is not None
            assert str(test_run.strategy_id) == fold["chosen_strategy_id"]
            assert test_run.date_from.isoformat() == fold["test_from"]
            assert test_run.date_to.isoformat() == fold["test_to"]
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
    """
    with TestClient(prepared()) as client:
        _, created = _launch(client)
        _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        before = session.scalar(select(func.count()).select_from(Backtest))
    finally:
        session.close()

    _drive(session_factory, tmp_path, created["id"])

    session = session_factory()
    try:
        assert session.scalar(select(func.count()).select_from(Backtest)) == before
        walk_forward = session.get(WalkForward, uuid.UUID(created["id"]))
        assert walk_forward is not None
        assert walk_forward.status.value == "done"
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
