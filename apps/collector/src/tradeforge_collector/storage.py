"""OHLCV on disk: Parquet, partitioned by symbol / timeframe / year (ADR-05).

**Why Parquet and not Postgres.** Candles are a columnar problem. A backtest reads
one symbol, one timeframe, a range of years, and every row of it — but only five of
its columns, in order. Parquet stores each column contiguously and compressed, so
that read touches a fraction of the bytes a row-oriented table would. And the
partitioning is not cosmetic: asking for 2024 opens the 2024 directory and never
looks at the other files. That is *partition pruning*, and it is the difference
between a backtest that starts instantly and one that scans a decade to find a year.

**Why the prices are `decimal128`, not `float64`.** Every OHLCV file in the industry
is float64, and this one is not. The rule the project follows is: *approximate where
you are estimating, be exact where you are accounting*. A moving average is an
estimate — computing it in float is fine, and the engine will. But a fill price is an
accounting fact: it is multiplied by a volume and added to an equity curve thousands
of times, and float error compounds through exactly that path. Storing the source
data exactly means the engine chooses when to approximate, instead of inheriting an
approximation it never asked for and cannot undo.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds

from tradeforge_collector.source import Candle

# Scale 10 matches the PRICE type in packages/db: one definition of "a price" across
# the file, the wire and the table.
PRICE = pa.decimal128(20, 10)

OHLCV_SCHEMA = pa.schema(
    [
        # Microseconds, tz-aware. Arrow stores the instant; the tz keeps a reader from
        # having to guess, which is the whole failure this project keeps designing out.
        pa.field("time", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("open", PRICE, nullable=False),
        pa.field("high", PRICE, nullable=False),
        pa.field("low", PRICE, nullable=False),
        pa.field("close", PRICE, nullable=False),
        pa.field("tick_volume", pa.int64(), nullable=False),
        pa.field("spread", pa.int32(), nullable=False),
        pa.field("real_volume", pa.int64(), nullable=False),
        # Partition keys. They live in the directory names, not in the file bodies.
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("year", pa.int32(), nullable=False),
    ]
)

PARTITION_KEYS = ["symbol", "timeframe", "year"]


def dataset_path(root: Path, symbol: str, timeframe: str) -> str:
    """The directory that holds one symbol's bars at one timeframe.

    This is what goes into `datasets.parquet_path`: a *directory*, not a file. The
    year partitions underneath it come and go as the backfill is re-run.
    """
    return (root / f"symbol={symbol}" / f"timeframe={timeframe}").as_posix()


def to_table(symbol: str, timeframe: str, candles: list[Candle]) -> pa.Table:
    """Candles in, Arrow out."""
    return pa.table(
        {
            "time": [candle.time for candle in candles],
            "open": [candle.open for candle in candles],
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
            "tick_volume": [candle.tick_volume for candle in candles],
            "spread": [candle.spread for candle in candles],
            "real_volume": [candle.real_volume for candle in candles],
            "symbol": [symbol] * len(candles),
            "timeframe": [timeframe] * len(candles),
            "year": [candle.time.year for candle in candles],
        },
        schema=OHLCV_SCHEMA,
    )


def write_candles(root: Path, symbol: str, timeframe: str, candles: list[Candle]) -> str:
    """Write the bars, replacing whatever those partitions held before.

    `delete_matching` is what makes a re-backfill idempotent: the year directories this
    run produces are wiped first, so downloading 2024 again *replaces* 2024 instead of
    appending a second, duplicate copy of it. Years this run does not touch are left
    alone — re-fetching 2024 must not delete the 2023 you already have.

    Returns the dataset directory, for the catalogue to record.
    """
    if not candles:
        raise ValueError(f"no candles to write for {symbol} {timeframe}")

    ds.write_dataset(
        to_table(symbol, timeframe, candles),
        base_dir=root,
        format="parquet",
        partitioning=ds.partitioning(
            pa.schema([OHLCV_SCHEMA.field(key) for key in PARTITION_KEYS]), flavor="hive"
        ),
        existing_data_behavior="delete_matching",
        basename_template="part-{i}.parquet",
    )

    return dataset_path(root, symbol, timeframe)


def read_candles(root: Path, symbol: str, timeframe: str) -> list[Candle]:
    """Read a symbol's bars back, in order.

    Sorted explicitly: a dataset is a set of files, and the order they are listed in is
    the filesystem's business, not ours. Candles that arrive out of order would make the
    engine's notion of "the previous bar" a lie.
    """
    directory = Path(dataset_path(root, symbol, timeframe))
    if not directory.exists():
        return []

    table = ds.dataset(directory, format="parquet", partitioning="hive").to_table()
    table = table.sort_by("time")

    rows = table.to_pylist()
    return [
        Candle(
            time=_as_utc(row["time"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            tick_volume=row["tick_volume"],
            spread=row["spread"],
            real_volume=row["real_volume"],
        )
        for row in rows
    ]


def _as_utc(moment: dt.datetime) -> dt.datetime:
    """Arrow hands back an aware datetime; make the timezone explicitly `dt.UTC`."""
    return moment.astimezone(dt.UTC)


def normalise(value: Decimal, digits: int) -> Decimal:
    """Round a price to the instrument's number of digits.

    MT5 hands out `float`. `Decimal(1.10525)` would preserve the float's binary noise
    forever (`1.10524999999999997...`); quantising at the instrument's precision is what
    turns a float back into the number the market actually printed.
    """
    return value.quantize(Decimal(1).scaleb(-digits))
