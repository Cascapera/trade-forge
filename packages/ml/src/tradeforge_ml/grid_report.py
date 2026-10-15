"""Write the grid's report beside a base (ADR-0031, 14/10): `grid/<name>/report.json` for a
program and `report.md` for a person, with the hash of the file it read — the same file gives the
same report.
"""

import datetime as dt
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from tradeforge_ml.grid import MIN_TRADES, GridReport, Summary, cell_name, evaluate, grid_rows
from tradeforge_ml.model import Floor, Market, Split

SHOWN_MARKETS = 40
"""Markets the person's report lists, the best of their training years first."""


def name_of(plan: Split, floor: Floor | None, min_trades: int) -> str:
    """`grid-val2023-2024-floor5t2s-min30`."""
    last = (plan.validation_to - dt.timedelta(days=1)).year
    excluded = "-excl" if plan.excluded else ""
    floored = "" if floor is None else f"-floor{floor.min_ticks:g}t{floor.min_spreads:g}s"
    return f"grid-val{plan.validation_from.year}-{last}{excluded}{floored}-min{min_trades}"


def _short(market: str) -> str:
    """A market with its setup's id cut to eight characters, as a person reads it."""
    setup, chart = market.split(" ", 1)
    return f"{setup[:8]} {chart}"


def _row(name: str, summary: Summary) -> str:
    return (
        f"| {name} | {summary.trades:,} | {summary.win_rate:.1%} | {summary.mean_r:+.3f} "
        f"| {summary.total_r:+,.1f} | {summary.max_drawdown_r:,.1f} "
        f"| {summary.longest_losing_streak} |"
    )


RULE_HEADER = [
    "| rule | trades | won | mean R | total R | max drawdown R | longest losing run |",
    "|---|---|---|---|---|---|---|",
]


def _cells(report: GridReport, cells: Mapping[str, Summary]) -> list[str]:
    labels = ["as run" if target is None else f"{target:g}R" for target in report.targets]
    lines = ["| stop | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
    for stop in report.stops:
        means = (cells[cell_name(stop, target)].mean_r for target in report.targets)
        lines.append(f"| {stop:g}x | " + " | ".join(f"{mean:+.3f}" for mean in means) + " |")
    return [*lines, ""]


def markdown(report: GridReport, name: str, manifest: Mapping[str, Any]) -> str:
    years = sorted({year for rule in report.rules for year in rule.by_year})
    traded = [choice for choice in report.markets if choice.cell is not None]
    lines = [
        f"# {name}",
        "",
        f"{report.entries:,} entries under {len(report.stops)} stops and "
        f"{len(report.targets)} targets: {report.left_open:,} left out (a stop still open when "
        f"the bars ran out), {report.embargoed:,} by the embargo. Chosen on "
        f"{report.training_entries:,} before {manifest['validation_from']}, judged on "
        f"{report.validation_entries:,} from then to {manifest['validation_to']}.",
        "",
        *(
            []
            if report.floor is None
            else [
                f"Stop floor: {report.floor.min_ticks:g} ticks and {report.floor.min_spreads:g} "
                "spreads, stop by stop. Trades it left out: "
                + ", ".join(f"{stop}x {count:,}" for stop, count in report.below_floor.items())
                + ".",
                "",
            ]
        ),
        "Refused by the broker (no fill with that stop): "
        + ", ".join(f"{stop}x {count:,}" for stop, count in report.refused.items())
        + ".",
        "",
        "## The rules on validation",
        "",
        *RULE_HEADER,
        *(_row(rule.name, rule.validation) for rule in report.rules),
        "",
        "Every rule books each trade at one risk, in the order they closed. Not trading is 0.",
        "",
        "## Validation, year by year (mean R / total R)",
        "",
        "| rule | " + " | ".join(str(year) for year in years) + " |",
        "|---|" + "---|" * len(years),
        *(
            f"| {rule.name} | "
            + " | ".join(
                f"{rule.by_year[year].mean_r:+.3f} / {rule.by_year[year].total_r:+,.1f}"
                if year in rule.by_year
                else "-"
                for year in years
            )
            + " |"
            for rule in report.rules
        ),
        "",
        "## The rules on the training years (where they were chosen: flattering)",
        "",
        *RULE_HEADER,
        *(_row(rule.name, rule.training) for rule in report.rules),
        "",
        "## Mean R of every cell, training years",
        "",
        *_cells(report, report.training_cells),
        "## Mean R of every cell, validation",
        "",
        *_cells(report, report.validation_cells),
        f"## A cell per market: {len(traded)} of {len(report.markets)} markets traded",
        "",
        f"At least {report.min_trades} training trades for a cell to be chosen; a market none "
        "of whose cells ended positive is not traded. The best training years first.",
        "",
        "| market | cell | training: trades, mean R, total R | validation: trades, mean R, "
        "total R | drawdown | losing run | as run, validation total R |",
        "|---|---|---|---|---|---|---|",
        *(
            f"| {_short(choice.market)} | {choice.cell} | {choice.training.trades:,}, "
            f"{choice.training.mean_r:+.3f}, {choice.training.total_r:+,.1f} "
            f"| {choice.validation.trades:,}, {choice.validation.mean_r:+.3f}, "
            f"{choice.validation.total_r:+,.1f} | {choice.validation.max_drawdown_r:,.1f} "
            f"| {choice.validation.longest_losing_streak} "
            f"| {choice.as_run_validation.total_r:+,.1f} |"
            for choice in traded[:SHOWN_MARKETS]
        ),
        "",
    ]
    return "\n".join(lines)


def write_grid(
    base: Path,
    plan: Split,
    floor: Floor | None = None,
    markets: Mapping[str, Market] | None = None,
    min_trades: int = MIN_TRADES,
) -> tuple[str, GridReport]:
    """Score `base`'s grid and write the report under `grid/` — of the trades above `floor`, read
    with the `markets`' ticks."""
    path = base / "grid.parquet"
    report = evaluate(grid_rows(pq.read_table(path), markets), plan, floor, min_trades)
    name = name_of(plan, floor, min_trades)
    manifest: dict[str, Any] = {
        "validation_from": plan.validation_from.date().isoformat(),
        "validation_to": plan.validation_to.date().isoformat(),
        "excluded_from_training": [
            [left.date().isoformat(), right.date().isoformat()] for left, right in plan.excluded
        ],
        "floor": None if floor is None else asdict(floor),
        "min_trades": min_trades,
        "grid_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
    }
    out = base / "grid" / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({"manifest": manifest, "report": asdict(report)}, indent=2, default=str)
    )
    (out / "report.md").write_text(markdown(report, name, manifest), encoding="utf-8")
    return name, report


__all__ = ["markdown", "name_of", "write_grid"]
