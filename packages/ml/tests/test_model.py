"""The first meta-label (ADR-0031): split by time with an embargo, judged by the R it keeps."""

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradeforge_ml.cli import span
from tradeforge_ml.features import FEATURE_NAMES
from tradeforge_ml.model import (
    Rows,
    Split,
    evaluate,
    fit,
    kept,
    probabilities,
    rows_of,
    split,
)
from tradeforge_ml.train import name_of, train

UTC = dt.UTC
PLAN = Split(dt.datetime(2023, 1, 1, tzinfo=UTC), dt.datetime(2025, 1, 1, tzinfo=UTC))
SIGNAL = FEATURE_NAMES.index("ret5_atr")


def _us(instant: dt.datetime) -> int:
    return int(instant.timestamp() * 1_000_000)


def synthetic(count: int, *, signal: bool, seed: int = 3) -> Rows:
    """Entries from 2016 to 2024; with `signal`, a trade wins when `ret5_atr` is high, mostly."""
    rng = np.random.default_rng(seed)
    start, end = _us(dt.datetime(2016, 1, 1, tzinfo=UTC)), _us(dt.datetime(2025, 1, 1, tzinfo=UTC))
    entry = np.sort(rng.integers(start, end, count))
    features = rng.normal(size=(count, len(FEATURE_NAMES)))
    features[rng.random((count, len(FEATURE_NAMES))) < 0.05] = math.nan  # some empty values
    chance = 1 / (1 + np.exp(-3 * np.nan_to_num(features[:, SIGNAL]))) if signal else 0.3
    won = rng.random(count) < chance
    return Rows(
        features=features,
        timeframe=rng.choice(["M15", "H1"], count).astype(np.str_),
        symbol=rng.choice(["EURUSD", "GBPUSD"], count).astype(np.str_),
        entry_us=entry,
        exit_us=entry + 3_600_000_000,
        r=np.where(won, 2.0, -1.0),
    )


class TestSplit:
    def test_training_is_before_validation_and_an_open_trade_across_it_is_embargoed(self) -> None:
        boundary = _us(PLAN.validation_from)
        rows = synthetic(2000, signal=False)
        rows.exit_us[0] = boundary + 1  # 2016's first trade still open in 2023
        training, validation, embargoed = split(rows, PLAN)

        assert embargoed == 1
        assert training.entry_us.max() < boundary
        assert training.exit_us.max() < boundary
        assert validation.entry_us.min() >= boundary
        assert len(training.r) + len(validation.r) + embargoed == len(rows.r)

    def test_an_excluded_span_leaves_training_and_only_training(self) -> None:
        rows = synthetic(4000, signal=False)
        left, right = dt.datetime(2020, 3, 1, tzinfo=UTC), dt.datetime(2020, 7, 1, tzinfo=UTC)
        cut = Split(PLAN.validation_from, PLAN.validation_to, ((left, right),))

        training, validation, _ = split(rows, cut)
        whole, same_validation, _ = split(rows, PLAN)

        inside = (training.entry_us >= _us(left)) & (training.entry_us < _us(right))
        assert not inside.any()
        assert len(whole.r) > len(training.r)
        assert np.array_equal(validation.entry_us, same_validation.entry_us)


class TestTheModel:
    def test_it_finds_a_planted_signal_and_keeps_more_r(self) -> None:
        _, report = evaluate(synthetic(6000, signal=True), PLAN)

        assert report.validation_auc is not None
        assert report.validation_auc > 0.8
        everything, *_, third = report.validation
        assert third.mean_r > everything.mean_r + 0.5
        assert report.weights[0][0] == "ret5_atr"
        assert report.weights[0][1] > 0

    def test_without_a_signal_it_ranks_no_better_than_a_coin(self) -> None:
        _, report = evaluate(synthetic(6000, signal=False), PLAN)

        assert report.validation_auc is not None
        assert abs(report.validation_auc - 0.5) < 0.06

    def test_validation_outcomes_never_reach_the_fit(self) -> None:
        """Rule 5: what validation's trades did cannot change the model judged on them."""
        rows = synthetic(3000, signal=True)
        training, validation, _ = split(rows, PLAN)
        flipped = Rows(
            rows.features,
            rows.timeframe,
            rows.symbol,
            rows.entry_us,
            rows.exit_us,
            np.where(rows.entry_us >= _us(PLAN.validation_from), -rows.r, rows.r),
        )
        again, _, _ = split(flipped, PLAN)

        assert np.allclose(
            probabilities(fit(training), validation), probabilities(fit(again), validation)
        )

    def test_the_symbol_is_not_an_input(self) -> None:
        rows = synthetic(3000, signal=True)
        renamed = Rows(
            rows.features,
            rows.timeframe,
            np.full(len(rows.r), "NOPE", dtype=np.str_),
            rows.entry_us,
            rows.exit_us,
            rows.r,
        )
        model = fit(rows)

        assert np.array_equal(probabilities(model, rows), probabilities(fit(renamed), renamed))


def test_the_best_share_is_taken_by_probability() -> None:
    rows = synthetic(10, signal=False)
    rows.r[:] = np.arange(10, dtype=np.float64)
    probability = np.arange(10, dtype=np.float64) / 10

    half = kept(rows, probability, 0.5)

    assert half.entries == 5
    assert half.mean_r == pytest.approx(7.0)  # the five most probable: R 5 to 9
    assert half.win_rate == 1.0


def _tables(rows: Rows) -> tuple[pa.Table, pa.Table]:
    count = len(rows.r)
    key = {
        "entry_id": [f"e{n}" for n in range(count)],
        "symbol": rows.symbol.tolist(),
        "timeframe": rows.timeframe.tolist(),
        "side": ["long"] * count,
        "entry_time": pa.array(rows.entry_us, type=pa.timestamp("us", tz="UTC")),
    }
    events = pa.table(
        {
            **key,
            "exit_time": pa.array(rows.exit_us, type=pa.timestamp("us", tz="UTC")),
            "r_multiple": rows.r,
        }
    )
    features = pa.table(
        {**key, **{name: rows.features[:, n] for n, name in enumerate(FEATURE_NAMES)}}
    )
    return events, features


class TestTheFiles:
    def test_rows_are_joined_one_to_one_and_mismatched_files_refused(self) -> None:
        rows = synthetic(50, signal=False)
        events, features = _tables(rows)

        joined = rows_of(events, features)
        assert np.array_equal(joined.r, rows.r)
        assert np.array_equal(joined.features, rows.features, equal_nan=True)

        with pytest.raises(ValueError, match="49 rows"):
            rows_of(events, features.slice(1))
        reordered = features.take(list(reversed(range(50))))
        with pytest.raises(ValueError, match="disagree"):
            rows_of(events, reordered)
        with pytest.raises(ValueError, match="spread_rel"):
            rows_of(events, features.drop_columns(["spread_rel"]))

    def test_training_writes_a_report_that_names_what_it_read(self, tmp_path: Path) -> None:
        events, features = _tables(synthetic(3000, signal=True))
        pq.write_table(events, tmp_path / "events.parquet")
        pq.write_table(features, tmp_path / "features.parquet")

        name, report = train(tmp_path, PLAN)

        assert name == name_of(PLAN) == "logistic-v2-val2023-2024"
        written = json.loads((tmp_path / "models" / name / "report.json").read_text())
        assert written["manifest"]["features_version"] == 2
        assert written["manifest"]["seed"] == 20261006
        assert len(written["manifest"]["events_sha256"]) == 64
        assert written["report"]["validation_entries"] == report.validation_entries
        text = (tmp_path / "models" / name / "report.md").read_text(encoding="utf-8")
        assert "ret5_atr" in text


def test_a_span_is_two_days_the_second_after_the_first() -> None:
    left, right = span("2023-01-01:2025-01-01")

    assert (left, right) == (PLAN.validation_from, PLAN.validation_to)
    assert name_of(Split(left, right, ((left, right),))).endswith("-excl")
