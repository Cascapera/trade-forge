"""Train a meta-label on a sweep's base and write what it did beside it (ADR-0031, rule 7):
`models/<name>/report.json` for a program and `report.md` for a person, with the seed, the
variables' version, the library's version and the hashes of the files it read — the same files
give the same report.

The model itself is not kept: this first one is judged, not used. The day one filters a live
setup it gets saved with the version it was trained on.
"""

import datetime as dt
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import sklearn

from tradeforge_ml.features import FEATURES_VERSION
from tradeforge_ml.model import KEEP, SEED, TARGETS, Kept, Kind, Report, Split, evaluate, rows_of


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def name_of(plan: Split, kind: Kind = "logistic") -> str:
    """`logistic-v2-val2023-2024`, and `-excl` when spans were left out of training."""
    last = (plan.validation_to - dt.timedelta(days=1)).year
    excluded = "-excl" if plan.excluded else ""
    return f"{kind}-v{FEATURES_VERSION}-val{plan.validation_from.year}-{last}{excluded}"


def _line(kept: Kept, everything: Kept) -> str:
    better = kept.mean_r - everything.mean_r
    return (
        f"| {kept.share:.0%} | {kept.entries:,} | {kept.mean_r:+.3f} | {better:+.3f} "
        f"| {kept.win_rate:.1%} | {kept.total_r:+,.0f} |"
    )


def _table(title: str, groups: dict[Any, list[Kept]]) -> list[str]:
    out = [
        f"## {title}",
        "",
        "| | entries | mean R, all | mean R, best half | gain |",
        "|---|---|---|---|---|",
    ]
    for label, (everything, half) in groups.items():
        out.append(
            f"| {label} | {everything.entries:,} | {everything.mean_r:+.3f} | {half.mean_r:+.3f} "
            f"| {half.mean_r - everything.mean_r:+.3f} |"
        )
    return [*out, ""]


def markdown(report: Report, name: str, manifest: dict[str, Any]) -> str:
    everything = report.validation[0]
    lines = [
        f"# {name}",
        "",
        f"Trained on {report.training_entries:,} entries before {manifest['validation_from']} "
        f"({report.embargoed:,} dropped by the embargo), judged on "
        f"{report.validation_entries:,} from then to {manifest['validation_to']}.",
        "",
        f"AUC: training {report.training_auc:.3f}, validation {report.validation_auc:.3f} "
        "(0.5 is a coin; a validation far below training is a model that memorised).",
        "",
        "## Accepting the best entries of validation",
        "",
        "| accepted | entries | mean R | gain over all | won | total R |",
        "|---|---|---|---|---|---|",
        *(_line(kept, everything) for kept in report.validation),
        "",
        *_table("By year", report.by_year),
        *_table("By chart", report.by_timeframe),
        *_table("By symbol", report.by_symbol),
        "## Under a target (read off each trade's MFE)",
        "",
        "| target | mean R, all | best half | best 30% | won, best half |",
        "|---|---|---|---|---|",
        *(
            f"| {name} | {every.mean_r:+.3f} | {half.mean_r:+.3f} | {third.mean_r:+.3f} "
            f"| {half.win_rate:.1%} |"
            for name, (every, half, third) in report.targets.items()
        ),
        "",
        "## Best half of each symbol, under each target",
        "",
        "| symbol | " + " | ".join(report.targets) + " |",
        "|---|" + "---|" * len(report.targets),
        *(
            f"| {symbol} | "
            + " | ".join(f"{pair[1].mean_r:+.3f}" for pair in by_target.values())
            + " |"
            for symbol, by_target in report.targets_by_symbol.items()
        ),
        "",
        (
            "## Weights (scaled inputs; positive raises the odds of a winner)"
            if report.kind == "logistic"
            else "## What it leans on (AUC lost when the input is shuffled)"
        ),
        "",
        "| input | weight |",
        "|---|---|",
        *(f"| {name} | {weight:+.4f} |" for name, weight in report.weights),
        "",
    ]
    return "\n".join(lines)


def train(sweep_dir: Path, plan: Split, kind: Kind = "logistic") -> tuple[str, Report]:
    """Fit on `sweep_dir`'s events and variables, and write the report under `models/`."""
    events_path, features_path = sweep_dir / "events.parquet", sweep_dir / "features.parquet"
    rows = rows_of(pq.read_table(events_path), pq.read_table(features_path))
    _, report = evaluate(rows, plan, kind)
    name = name_of(plan, kind)
    manifest: dict[str, Any] = {
        "model": kind,
        "targets_r": [target for target in TARGETS if target is not None],
        "seed": SEED,
        "features_version": FEATURES_VERSION,
        "sklearn": sklearn.__version__,
        "validation_from": plan.validation_from.date().isoformat(),
        "validation_to": plan.validation_to.date().isoformat(),
        "excluded_from_training": [
            [left.date().isoformat(), right.date().isoformat()] for left, right in plan.excluded
        ],
        "keep": list(KEEP),
        "events_sha256": _sha256(events_path),
        "features_sha256": _sha256(features_path),
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
    }
    out = sweep_dir / "models" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({"manifest": manifest, "report": asdict(report)}, indent=2, default=str)
    )
    (out / "report.md").write_text(markdown(report, name, manifest), encoding="utf-8")
    return name, report


__all__ = ["markdown", "name_of", "train"]
