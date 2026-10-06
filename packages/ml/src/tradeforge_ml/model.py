"""The first meta-label (ADR-0031): a logistic regression that says which of a setup's entries to
take, trained on earlier years and judged on later ones.

The setup still decides every entry; this only reads the market at the decision bar (`features`)
and says how likely the trade is to end above zero R. What it is judged on is not its accuracy but
the question the ADR asks: **do the entries it accepts make more R, on average, than all of them?**

⚠️ **Split by time, never by drawing rows.** A row drawn at random puts 2023 in the training of a
model "predicting" 2019. Training is every entry before `validation_from`; validation is from
there to `validation_to`. An entry of the training years whose trade was still open when the
validation began is dropped (the embargo): its outcome was decided by validation-year bars.

⚠️ **The threshold is chosen on validation, so validation is not a test.** The report says how the
model ranks validation entries — accepting the best half, the best third — which is how a
threshold would be picked. Whether that holds is a question for years neither has seen.

⚠️ **The market's variables only, not the symbol.** A model that knows the symbol can learn "skip
AUDNZD" from the training years and call it skill; the chart (`timeframe`) is in, as the
variables mean different things on M15 and on D1.

Pure: two Arrow tables in, a report out. `cli` reads and writes the files.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from tradeforge_ml.feature_export import KEY
from tradeforge_ml.features import FEATURE_NAMES, FEATURES_VERSION

SEED = 20261006
"""Fixed, so the same files give the same model (ADR-0031, rule 7)."""

KEEP = (1.0, 0.7, 0.5, 0.3)
"""The shares of validation entries the report accepts, best first by the model's probability."""

Floats = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Split:
    """Training before `validation_from`; validation from it to before `validation_to`."""

    validation_from: dt.datetime
    validation_to: dt.datetime
    excluded: tuple[tuple[dt.datetime, dt.datetime], ...] = ()
    """Spans of entry time left out of training (a sensitivity check, never of validation)."""


@dataclass
class Rows:
    """Entries as columns: the variables, the chart, the outcome, and where each one is."""

    features: Floats
    timeframe: npt.NDArray[np.str_]
    symbol: npt.NDArray[np.str_]
    entry_us: npt.NDArray[np.int64]
    exit_us: npt.NDArray[np.int64]
    r: Floats

    def take(self, mask: npt.NDArray[np.bool_]) -> Rows:
        return Rows(
            self.features[mask],
            self.timeframe[mask],
            self.symbol[mask],
            self.entry_us[mask],
            self.exit_us[mask],
            self.r[mask],
        )

    @property
    def won(self) -> npt.NDArray[np.int64]:
        return (self.r > 0).astype(np.int64)

    @property
    def year(self) -> npt.NDArray[np.int64]:
        return (
            self.entry_us.astype("datetime64[us]").astype("datetime64[Y]").astype(np.int64) + 1970
        )


def _us(instant: dt.datetime) -> int:
    return int(instant.timestamp() * 1_000_000)


def rows_of(events: pa.Table, features: pa.Table) -> Rows:
    """Join `events.parquet` and `features.parquet` row by row, refusing files that do not match."""
    if events.num_rows != features.num_rows:
        raise ValueError(f"{events.num_rows} events but {features.num_rows} rows of variables")
    for name in KEY:
        if not events.column(name).equals(features.column(name)):
            raise ValueError(f"events and variables disagree on {name}: not the same base")
    missing = [name for name in FEATURE_NAMES if name not in features.column_names]
    if missing:
        raise ValueError(f"variables v{FEATURES_VERSION} expected; missing {', '.join(missing)}")

    def floats(table: pa.Table, name: str) -> Floats:
        column = pc.cast(table.column(name), pa.float64())
        return np.asarray(pc.fill_null(column, math.nan).to_numpy(), dtype=np.float64)

    def instants(name: str) -> npt.NDArray[np.int64]:
        column = pc.cast(events.column(name), pa.timestamp("us", tz="UTC")).cast(pa.int64())
        return np.asarray(column.to_numpy(), dtype=np.int64)

    return Rows(
        features=np.column_stack([floats(features, name) for name in FEATURE_NAMES]),
        timeframe=np.asarray(events.column("timeframe").to_pylist(), dtype=np.str_),
        symbol=np.asarray(events.column("symbol").to_pylist(), dtype=np.str_),
        entry_us=instants("entry_time"),
        exit_us=instants("exit_time"),
        r=floats(events, "r_multiple"),
    )


def split(rows: Rows, plan: Split) -> tuple[Rows, Rows, int]:
    """Training and validation rows, and how many training rows the embargo dropped."""
    start, end = _us(plan.validation_from), _us(plan.validation_to)
    before = rows.entry_us < start
    embargoed = before & (rows.exit_us >= start)
    training = before & ~embargoed
    for left, right in plan.excluded:
        training &= ~((rows.entry_us >= _us(left)) & (rows.entry_us < _us(right)))
    validation = (rows.entry_us >= start) & (rows.entry_us < end)
    return rows.take(training), rows.take(validation), int(embargoed.sum())


def _matrix(rows: Rows) -> npt.NDArray[np.object_]:
    """The variables and the chart, as one table the pipeline's columns are picked from."""
    return np.column_stack([rows.features.astype(object), rows.timeframe.astype(object)])


def build() -> Pipeline:
    """Empty values filled with the training median and flagged; scaled; the chart one-hot."""
    numeric = list(range(len(FEATURE_NAMES)))
    prepare = ColumnTransformer(
        [
            (
                "variables",
                Pipeline(
                    [
                        ("fill", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scale", StandardScaler()),
                    ]
                ),
                numeric,
            ),
            ("chart", OneHotEncoder(handle_unknown="ignore"), [len(FEATURE_NAMES)]),
        ]
    )
    return Pipeline(
        [
            ("prepare", prepare),
            ("model", LogisticRegression(max_iter=2000, random_state=SEED)),
        ]
    )


def fit(training: Rows) -> Pipeline:
    pipeline = build()
    pipeline.fit(_matrix(training), training.won)
    return pipeline


def probabilities(pipeline: Pipeline, rows: Rows) -> Floats:
    return np.asarray(pipeline.predict_proba(_matrix(rows))[:, 1], dtype=np.float64)


@dataclass
class Kept:
    share: float
    entries: int
    mean_r: float
    total_r: float
    win_rate: float


def kept(rows: Rows, probability: Floats, share: float) -> Kept:
    """The best `share` of `rows` by the model's probability: how many, and what they made."""
    count = max(1, round(len(rows.r) * share))
    best = np.argsort(-probability, kind="stable")[:count]
    r = rows.r[best]
    return Kept(share, count, float(r.mean()), float(r.sum()), float((r > 0).mean()))


def _auc(won: npt.NDArray[np.int64], probability: Floats) -> float | None:
    """The chance a winner is ranked above a loser; none when only one of the two is there."""
    return float(roc_auc_score(won, probability)) if len(set(won.tolist())) > 1 else None


_POSITION = re.compile(r"\bx(\d+)")
"""How the pipeline names its inputs: by position (`x23`, `missingindicator_x23`, `x32_M15`)."""


def coefficients(pipeline: Pipeline) -> list[tuple[str, float]]:
    """Each input's weight on the scaled scale, largest first: a positive one raises the odds."""
    inputs = [*FEATURE_NAMES, "timeframe"]

    def readable(name: str) -> str:
        named = _POSITION.sub(lambda match: inputs[int(match.group(1))], name.split("__", 1)[1])
        return named.replace("missingindicator_", "empty: ")

    names = pipeline.named_steps["prepare"].get_feature_names_out()
    weights = pipeline.named_steps["model"].coef_[0]
    pairs = [(readable(str(n)), float(w)) for n, w in zip(names, weights, strict=True)]
    return sorted(pairs, key=lambda pair: -abs(pair[1]))


@dataclass
class Report:
    """What the model did, in the terms the ADR judges it by."""

    training_entries: int
    embargoed: int
    validation_entries: int
    training_auc: float | None
    validation_auc: float | None
    validation: list[Kept]
    by_year: dict[int, list[Kept]] = field(default_factory=dict)
    by_symbol: dict[str, list[Kept]] = field(default_factory=dict)
    by_timeframe: dict[str, list[Kept]] = field(default_factory=dict)
    weights: list[tuple[str, float]] = field(default_factory=list)


def _groups(
    rows: Rows, probability: Floats, labels: npt.NDArray[Any], shares: Sequence[float]
) -> dict[Any, list[Kept]]:
    """Accepting within each group, so a share means the same in a busy year and a quiet one."""
    out: dict[Any, list[Kept]] = {}
    for label in sorted(set(labels.tolist())):
        mask = labels == label
        part = rows.take(mask)
        out[label] = [kept(part, probability[mask], share) for share in shares]
    return out


def evaluate(rows: Rows, plan: Split) -> tuple[Pipeline, Report]:
    """Fit on the training years and report on validation."""
    training, validation, embargoed = split(rows, plan)
    if len(training.r) == 0 or len(validation.r) == 0:
        raise ValueError("no training or no validation entries for these dates")
    pipeline = fit(training)
    seen = probabilities(pipeline, training)
    unseen = probabilities(pipeline, validation)
    shares = (1.0, 0.5)
    return pipeline, Report(
        training_entries=len(training.r),
        embargoed=embargoed,
        validation_entries=len(validation.r),
        training_auc=_auc(training.won, seen),
        validation_auc=_auc(validation.won, unseen),
        validation=[kept(validation, unseen, share) for share in KEEP],
        by_year=_groups(validation, unseen, validation.year, shares),
        by_symbol=_groups(validation, unseen, validation.symbol, shares),
        by_timeframe=_groups(validation, unseen, validation.timeframe, shares),
        weights=coefficients(pipeline),
    )


__all__ = [
    "KEEP",
    "SEED",
    "Kept",
    "Report",
    "Rows",
    "Split",
    "build",
    "coefficients",
    "evaluate",
    "fit",
    "kept",
    "probabilities",
    "rows_of",
    "split",
]
