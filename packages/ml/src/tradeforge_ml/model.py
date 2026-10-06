"""The meta-label (ADR-0031): a model that says which of a setup's entries to take, trained on
earlier years and judged on later ones — a logistic regression, the bar, or gradient boosting.

The setup still decides every entry; this only reads the market at the decision bar (`features`)
and says how likely the trade is to end above zero R. What it is judged on is not its accuracy but
the question the ADR asks: **do the entries it accepts make more R, on average, than all of them?**
And, from each entry's maximum favourable excursion, **what they would make with a target**
(`with_target`): the base runs without one, so every target is read off how far the trade went.

⚠️ **Split by time, never by drawing rows.** A row drawn at random puts 2023 in the training of a
model "predicting" 2019. Training is every entry before `validation_from`; validation is from
there to `validation_to`. An entry of the training years whose trade was still open when the
validation began is dropped (the embargo): its outcome was decided by validation-year bars.

⚠️ **Validation is not a test.** The report ranks validation entries under every model, share and
target — which is how a choice is made, and every choice made there flatters it. Whether a choice
holds is a question for years neither has seen.

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
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from tradeforge_ml.feature_export import KEY
from tradeforge_ml.features import FEATURE_NAMES, FEATURES_VERSION, STEP

SEED = 20261006
"""Fixed, so the same files give the same model (ADR-0031, rule 7)."""

KEEP = (1.0, 0.7, 0.5, 0.3)
"""The shares of validation entries the report accepts, best first by the model's probability."""

TARGETS: tuple[float | None, ...] = (None, 1.0, 2.0, 3.0, 5.0)
"""The targets, in R, every accepted share is also scored under; `None` is the trade as it ran."""

IMPORTANCE_SAMPLE = 50_000
"""Validation rows a boosted model's permutation importance is measured on: enough for a stable
AUC, few enough that thirty-odd inputs times three shuffles stays a minute."""

Kind = Literal["logistic", "boosting"]
KINDS: tuple[Kind, ...] = ("logistic", "boosting")

CHARTS = tuple(STEP)
"""Every chart a row can be on, one-hot in this order — fixed, so a model sees the same columns
whatever charts its training happened to hold."""

INPUTS = (*FEATURE_NAMES, *(f"timeframe_{chart}" for chart in CHARTS))
"""The design matrix's columns, by name."""

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
    mfe_r: Floats
    """How far the trade went in its favour, in R of its risk (a price ratio)."""
    cost_r: Floats
    """What it paid — costs and swap — in R: its result as a price ratio less its R in money."""

    def take(self, mask: npt.NDArray[np.bool_]) -> Rows:
        return Rows(
            self.features[mask],
            self.timeframe[mask],
            self.symbol[mask],
            self.entry_us[mask],
            self.exit_us[mask],
            self.r[mask],
            self.mfe_r[mask],
            self.cost_r[mask],
        )

    @property
    def won(self) -> npt.NDArray[np.int64]:
        return (self.r > 0).astype(np.int64)

    @property
    def year(self) -> npt.NDArray[np.int64]:
        return (
            self.entry_us.astype("datetime64[us]").astype("datetime64[Y]").astype(np.int64) + 1970
        )


def with_target(rows: Rows, target: float | None) -> Floats:
    """Each trade's R under a `target` it did not have (`excursion.with_target`): one whose MFE
    reached it closes there, less what it paid; one that did not ends as it did. An MFE can only
    err low, so a derived hit is one the bars prove."""
    if target is None:
        return rows.r
    return np.where(rows.mfe_r >= target, target - rows.cost_r, rows.r)


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

    r = floats(events, "r_multiple")
    entry, exit_, stop = (
        floats(events, name) for name in ("entry_price", "exit_price", "stop_loss")
    )
    sign = np.where(np.asarray(events.column("side").to_pylist()) == "long", 1.0, -1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        gross_r = sign * (exit_ - entry) / np.abs(entry - stop)
    return Rows(
        features=np.column_stack([floats(features, name) for name in FEATURE_NAMES]),
        timeframe=np.asarray(events.column("timeframe").to_pylist(), dtype=np.str_),
        symbol=np.asarray(events.column("symbol").to_pylist(), dtype=np.str_),
        entry_us=instants("entry_time"),
        exit_us=instants("exit_time"),
        r=r,
        mfe_r=floats(events, "mfe_r"),
        cost_r=gross_r - r,
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


def design(rows: Rows) -> Floats:
    """The variables, then the chart one-hot over `CHARTS`: one float matrix both models read."""
    charts = (rows.timeframe[:, None] == np.asarray(CHARTS)[None, :]).astype(np.float64)
    return np.column_stack([rows.features, charts])


def build(kind: Kind = "logistic") -> Pipeline:
    """`logistic`: empty values filled with the training median and flagged, then scaled.
    `boosting`: trees read empty values as they are, so nothing is filled or scaled."""
    numeric = list(range(len(FEATURE_NAMES)))
    charts = list(range(len(FEATURE_NAMES), len(INPUTS)))
    if kind == "boosting":
        return Pipeline(
            [
                (
                    "model",
                    HistGradientBoostingClassifier(
                        max_iter=200,
                        learning_rate=0.05,
                        max_leaf_nodes=31,
                        min_samples_leaf=500,
                        l2_regularization=1.0,
                        early_stopping=False,
                        random_state=SEED,
                    ),
                )
            ]
        )
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
            ("chart", "passthrough", charts),
        ]
    )
    return Pipeline(
        [
            ("prepare", prepare),
            ("model", LogisticRegression(max_iter=2000, random_state=SEED)),
        ]
    )


def fit(training: Rows, kind: Kind = "logistic") -> Pipeline:
    pipeline = build(kind)
    pipeline.fit(design(training), training.won)
    return pipeline


def probabilities(pipeline: Pipeline, rows: Rows) -> Floats:
    return np.asarray(pipeline.predict_proba(design(rows))[:, 1], dtype=np.float64)


@dataclass
class Kept:
    share: float
    entries: int
    mean_r: float
    total_r: float
    win_rate: float


def kept(rows: Rows, probability: Floats, share: float, r: Floats | None = None) -> Kept:
    """The best `share` of `rows` by the model's probability: how many, and what they made —
    as they ran, or under the outcomes `r` (a target's)."""
    outcome = rows.r if r is None else r
    count = max(1, round(len(outcome) * share))
    best = np.argsort(-probability, kind="stable")[:count]
    chosen = outcome[best]
    return Kept(share, count, float(chosen.mean()), float(chosen.sum()), float((chosen > 0).mean()))


def _auc(won: npt.NDArray[np.int64], probability: Floats) -> float | None:
    """The chance a winner is ranked above a loser; none when only one of the two is there."""
    return float(roc_auc_score(won, probability)) if len(set(won.tolist())) > 1 else None


_POSITION = re.compile(r"(?<![A-Za-z0-9])x(\d+)")
"""How the pipeline names its inputs: by position (`x23`, `missingindicator_x23`)."""


def coefficients(pipeline: Pipeline) -> list[tuple[str, float]]:
    """Each input's weight on the scaled scale, largest first: a positive one raises the odds."""

    def readable(name: str) -> str:
        named = _POSITION.sub(lambda match: INPUTS[int(match.group(1))], name.split("__", 1)[1])
        return named.replace("missingindicator_", "empty: ")

    names = pipeline.named_steps["prepare"].get_feature_names_out()
    weights = pipeline.named_steps["model"].coef_[0]
    pairs = [(readable(str(n)), float(w)) for n, w in zip(names, weights, strict=True)]
    return sorted(pairs, key=lambda pair: -abs(pair[1]))


def importances(pipeline: Pipeline, rows: Rows) -> list[tuple[str, float]]:
    """How much the AUC falls when each input is shuffled, largest first — a boosted model has no
    weight to read, so it is asked what it leans on. Measured on validation, a sample of it."""
    rng = np.random.default_rng(SEED)
    sample = rng.choice(len(rows.r), size=min(IMPORTANCE_SAMPLE, len(rows.r)), replace=False)
    matrix, won = design(rows)[sample], rows.won[sample]
    if len(set(won.tolist())) < 2:  # noqa: PLR2004 — a winner and a loser
        return []
    measured = permutation_importance(
        pipeline, matrix, won, scoring="roc_auc", n_repeats=3, random_state=SEED
    )
    pairs = [
        (name, float(drop)) for name, drop in zip(INPUTS, measured.importances_mean, strict=True)
    ]
    return sorted(pairs, key=lambda pair: -pair[1])


def _target_name(target: float | None) -> str:
    return "as run" if target is None else f"{target:g}R"


@dataclass
class Report:
    """What the model did, in the terms the ADR judges it by."""

    kind: str
    training_entries: int
    embargoed: int
    validation_entries: int
    training_auc: float | None
    validation_auc: float | None
    validation: list[Kept]
    by_year: dict[int, list[Kept]] = field(default_factory=dict)
    by_symbol: dict[str, list[Kept]] = field(default_factory=dict)
    by_timeframe: dict[str, list[Kept]] = field(default_factory=dict)
    targets: dict[str, list[Kept]] = field(default_factory=dict)
    """Per target: all of validation, its best half, its best third."""
    targets_by_symbol: dict[str, dict[str, list[Kept]]] = field(default_factory=dict)
    """Per symbol, per target: all and the best half."""
    weights: list[tuple[str, float]] = field(default_factory=list)
    """Coefficients (logistic) or the AUC each input's shuffle costs (boosting)."""


def _groups(
    rows: Rows,
    probability: Floats,
    labels: npt.NDArray[Any],
    shares: Sequence[float],
    target: float | None = None,
) -> dict[Any, list[Kept]]:
    """Accepting within each group, so a share means the same in a busy year and a quiet one."""
    out: dict[Any, list[Kept]] = {}
    for label in sorted(set(labels.tolist())):
        mask = labels == label
        part = rows.take(mask)
        outcome = with_target(part, target)
        out[label] = [kept(part, probability[mask], share, outcome) for share in shares]
    return out


def evaluate(rows: Rows, plan: Split, kind: Kind = "logistic") -> tuple[Pipeline, Report]:
    """Fit on the training years and report on validation."""
    training, validation, embargoed = split(rows, plan)
    if len(training.r) == 0 or len(validation.r) == 0:
        raise ValueError("no training or no validation entries for these dates")
    pipeline = fit(training, kind)
    seen = probabilities(pipeline, training)
    unseen = probabilities(pipeline, validation)
    halves = (1.0, 0.5)
    targets = {
        _target_name(target): [
            kept(validation, unseen, share, with_target(validation, target))
            for share in (1.0, 0.5, 0.3)
        ]
        for target in TARGETS
    }
    by_symbol_targets: dict[str, dict[str, list[Kept]]] = {}
    for target in TARGETS:
        groups = _groups(validation, unseen, validation.symbol, halves, target)
        for symbol, pair in groups.items():
            by_symbol_targets.setdefault(symbol, {})[_target_name(target)] = pair
    return pipeline, Report(
        kind=kind,
        training_entries=len(training.r),
        embargoed=embargoed,
        validation_entries=len(validation.r),
        training_auc=_auc(training.won, seen),
        validation_auc=_auc(validation.won, unseen),
        validation=[kept(validation, unseen, share) for share in KEEP],
        by_year=_groups(validation, unseen, validation.year, halves),
        by_symbol=_groups(validation, unseen, validation.symbol, halves),
        by_timeframe=_groups(validation, unseen, validation.timeframe, halves),
        targets=targets,
        targets_by_symbol=by_symbol_targets,
        weights=(
            coefficients(pipeline) if kind == "logistic" else importances(pipeline, validation)
        ),
    )


__all__ = [
    "CHARTS",
    "INPUTS",
    "KEEP",
    "KINDS",
    "SEED",
    "TARGETS",
    "Kept",
    "Kind",
    "Report",
    "Rows",
    "Split",
    "build",
    "coefficients",
    "design",
    "evaluate",
    "fit",
    "importances",
    "kept",
    "probabilities",
    "rows_of",
    "split",
    "with_target",
]
