"""Write a sweep's variables beside its event base (ADR-0031): `features.parquet`, one row per row
of `events.parquet`, in the same order and with its key, so the two join one to one.

Read-only on the candles: the bars are the collector's Parquet (`data/ohlcv`), read once per market
and chart straight into columns, and every event of that chart is read off the same arrays
(`features.Market`).

⚠️ **The files, not the collector's code.** A shared package may not import a deployable app
(`tests/test_architecture.py`), so this reads the layout the collector writes and the database
records as `datasets.parquet_path` — `symbol=<s>/timeframe=<t>/year=<y>/*.parquet`, with `time`,
`open`, `high`, `low`, `close`, `tick_volume` and `spread` — the collector's published contract.

What a point is in price (`10 ** -digits`) is the instrument's, in the database: the command reads
it there (`points_of`) and hands it in, so this module still touches files only.
"""

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from tradeforge_ml.features import FEATURE_NAMES, FEATURES_VERSION, STEP, Market, features_at

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

MarketSource = Callable[[str, str], Market | None]
"""A chart's bars by (symbol, timeframe), or `None` when there are none on disk."""


def read_market(
    ohlcv: Path, symbol: str, timeframe: str, point: float | None = None
) -> Market | None:
    """One chart's bars from the collector's Parquet, as columns in time order; `point` what one
    point of its spread is in price, when known."""
    directory = ohlcv / f"symbol={symbol}" / f"timeframe={timeframe}"
    if not directory.exists():
        return None
    table = ds.dataset(directory, format="parquet", partitioning="hive").to_table(
        columns=["time", "open", "high", "low", "close", "tick_volume", "spread"]
    )
    if table.num_rows == 0:
        return None
    table = table.sort_by("time")
    times = pc.cast(table.column("time"), pa.timestamp("us", tz="UTC")).cast(pa.int64())

    def floats(name: str) -> npt.NDArray[np.float64]:
        return np.asarray(pc.cast(table.column(name), pa.float64()).to_numpy(), dtype=np.float64)

    return Market.from_arrays(
        times=np.asarray(times.to_numpy(), dtype=np.int64),
        step=STEP[timeframe],
        open_=floats("open"),
        high=floats("high"),
        low=floats("low"),
        close=floats("close"),
        volume=floats("tick_volume"),
        spread=floats("spread"),
        point=point,
    )


@dataclass
class FeatureReport:
    events: int = 0
    markets: int = 0
    without_bars: int = 0
    """Events whose chart has no candles on disk: their variables are all empty."""
    missing_markets: list[str] = field(default_factory=list)


def _float(value: Any) -> float | None:  # noqa: ANN401 — a Parquet cell, decimal or float
    return None if value is None else float(value)


def features_table(events: pa.Table, markets_of: MarketSource) -> tuple[pa.Table, FeatureReport]:
    """The variables of every event of `events`, in its order."""
    report = FeatureReport(events=events.num_rows)
    rows = events.select([*KEY, "entry_price", "stop_loss"]).to_pylist()
    markets: dict[tuple[str, str], Market | None] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        chart = (row["symbol"], row["timeframe"])
        if chart not in markets:
            markets[chart] = markets_of(*chart)
            if markets[chart] is None:
                report.missing_markets.append(f"{chart[0]} {chart[1]}")
            else:
                report.markets += 1
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


def write_features(
    sweep_dir: Path, ohlcv: Path, points: Mapping[str, float] | None = None
) -> FeatureReport:
    """Read `sweep_dir/events.parquet`, write `features.parquet` and `features.json` beside it.
    `points` is each symbol's point in price; a symbol missing from it has no spread in price."""
    events = pq.read_table(sweep_dir / "events.parquet")
    known = points or {}
    table, report = features_table(
        events,
        lambda symbol, timeframe: read_market(ohlcv, symbol, timeframe, known.get(symbol)),
    )
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


__all__ = [
    "FEATURE_SCHEMA",
    "KEY",
    "FeatureReport",
    "features_table",
    "read_market",
    "write_features",
]
