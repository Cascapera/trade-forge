"""Write a sweep's variables beside its event base (ADR-0031): `features.parquet`, one row per row
of `events.parquet`, in the same order and with its key, so the two join one to one.

Read-only on the candles: the bars are the collector's Parquet (`data/ohlcv`), read once per market
and chart, and every event of that chart is read off the same arrays (`features.Market`).
"""

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from tradeforge_collector import read_candles
from tradeforge_collector.timeframes import step
from tradeforge_engine.domain import Candle
from tradeforge_ml.features import FEATURE_NAMES, FEATURES_VERSION, Market, features_at

KEY = ("entry_id", "symbol", "timeframe", "side", "entry_time")
"""What a row of `features.parquet` repeats of its event, to be joined back."""

FEATURE_SCHEMA = pa.schema(
    [
        ("entry_id", pa.string()),
        ("symbol", pa.string()),
        ("timeframe", pa.string()),
        ("side", pa.string()),
        ("entry_time", pa.timestamp("us", tz="UTC")),
        *((name, pa.float64()) for name in FEATURE_NAMES),
    ]
)

CandleSource = Callable[[str, str], Sequence[Candle]]


@dataclass
class FeatureReport:
    events: int = 0
    markets: int = 0
    without_bars: int = 0
    """Events whose chart has no candles on disk: their variables are all empty."""
    missing_markets: list[str] = field(default_factory=list)


def _float(value: Any) -> float | None:  # noqa: ANN401 — a Parquet cell, decimal or float
    return None if value is None else float(value)


def features_table(events: pa.Table, candles: CandleSource) -> tuple[pa.Table, FeatureReport]:
    """The variables of every event of `events`, in its order."""
    report = FeatureReport(events=events.num_rows)
    rows = events.select([*KEY, "entry_price", "stop_loss"]).to_pylist()
    markets: dict[tuple[str, str], Market | None] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        chart = (row["symbol"], row["timeframe"])
        if chart not in markets:
            bars = candles(*chart)
            markets[chart] = Market.of(bars, step(chart[1])) if bars else None
            if bars:
                report.markets += 1
            else:
                report.missing_markets.append(f"{chart[0]} {chart[1]}")
        market = markets[chart]
        key = {name: row[name] for name in KEY}
        if market is None:
            report.without_bars += 1
            out.append({**key, **dict.fromkeys(FEATURE_NAMES)})
            continue
        values = features_at(
            market,
            entry_time=row["entry_time"],
            side=row["side"],
            entry_price=float(row["entry_price"]),
            stop_loss=_float(row["stop_loss"]),
        )
        out.append({**key, **values})
    return pa.Table.from_pylist(out, schema=FEATURE_SCHEMA), report


def write_features(sweep_dir: Path, ohlcv: Path) -> FeatureReport:
    """Read `sweep_dir/events.parquet`, write `features.parquet` and `features.json` beside it."""
    events = pq.read_table(sweep_dir / "events.parquet")

    def candles(symbol: str, timeframe: str) -> Sequence[Candle]:
        return read_candles(ohlcv, symbol, timeframe)

    table, report = features_table(events, candles)
    pq.write_table(table, sweep_dir / "features.parquet", compression="zstd")
    manifest = {
        "features_version": FEATURES_VERSION,
        "features": list(FEATURE_NAMES),
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "events": report.events,
        "markets": report.markets,
        "without_bars": report.without_bars,
        "missing_markets": report.missing_markets,
        "events_sha256": hashlib.sha256((sweep_dir / "events.parquet").read_bytes()).hexdigest(),
        "features_sha256": hashlib.sha256(
            (sweep_dir / "features.parquet").read_bytes()
        ).hexdigest(),
    }
    (sweep_dir / "features.json").write_text(json.dumps(manifest, indent=2))
    return report


__all__ = ["FEATURE_SCHEMA", "KEY", "FeatureReport", "features_table", "write_features"]
