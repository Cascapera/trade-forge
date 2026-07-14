"""Example instruments, so a fresh clone has something to backtest against.

Seeds are **not** migrations. A migration describes the shape of the database and
must run exactly once, in order, on every environment including production. Seed
data is convenience: it must be re-runnable, it is dev-only, and it has no business
in the schema's version history. Mixing the two is how a production deploy ends up
inserting four fake symbols.

The numbers below are plausible placeholders. The collector overwrites them with
the truth from MT5 `symbol_info` on the first backfill (PR-102) — which is why the
writer upserts on `symbol` instead of failing on conflict.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from tradeforge_db.models import AssetClass, Instrument


@dataclass(frozen=True, slots=True)
class InstrumentSeed:
    """Pure data: no session, no I/O — so the table below is unit-testable."""

    symbol: str
    name: str
    asset_class: AssetClass
    currency_quote: str
    tick_size: Decimal
    tick_value: Decimal
    contract_size: Decimal
    digits: int
    exchange: str | None = None
    currency_base: str | None = None


INSTRUMENT_SEEDS: tuple[InstrumentSeed, ...] = (
    # Forex: one standard lot is 100 000 units of the base currency, quoted to five
    # decimals. One tick (0.00001) on that lot is worth $1.
    InstrumentSeed(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    ),
    InstrumentSeed(
        symbol="GBPUSD",
        name="Great Britain Pound vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="GBP",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    ),
    # A stock has no base currency, trades in whole shares, and moves in cents. Same
    # engine, same table, entirely different arithmetic — which is the point of
    # keeping these numbers in the database instead of in the engine.
    InstrumentSeed(
        symbol="AAPL",
        name="Apple Inc.",
        asset_class=AssetClass.STOCK,
        exchange="NASDAQ",
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal("1"),
        digits=2,
    ),
    InstrumentSeed(
        symbol="US500",
        name="S&P 500 Index",
        asset_class=AssetClass.INDEX,
        exchange="CME",
        currency_quote="USD",
        tick_size=Decimal("0.1"),
        tick_value=Decimal("0.1"),
        contract_size=Decimal("1"),
        digits=1,
    ),
)


def seed_instruments(session: Session, seeds: tuple[InstrumentSeed, ...] = INSTRUMENT_SEEDS) -> int:
    """Insert the example instruments, updating any that already exist.

    Idempotent by construction: running it twice leaves the same four rows, and it
    never collides with a symbol the collector has already written for real. Returns
    the number of rows written.
    """
    if not seeds:
        return 0

    rows = [
        {
            "symbol": seed.symbol,
            "name": seed.name,
            "asset_class": seed.asset_class,
            "exchange": seed.exchange,
            "currency_base": seed.currency_base,
            "currency_quote": seed.currency_quote,
            "tick_size": seed.tick_size,
            "tick_value": seed.tick_value,
            "contract_size": seed.contract_size,
            "digits": seed.digits,
        }
        for seed in seeds
    ]

    statement = insert(Instrument).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[Instrument.symbol],
        set_={
            column: statement.excluded[column]
            for column in (
                "name",
                "asset_class",
                "exchange",
                "currency_base",
                "currency_quote",
                "tick_size",
                "tick_value",
                "contract_size",
                "digits",
            )
        },
    )
    session.execute(statement)
    # Every seed is written on every run — inserted the first time, updated after —
    # so the count is simply how many we were given.
    return len(rows)
