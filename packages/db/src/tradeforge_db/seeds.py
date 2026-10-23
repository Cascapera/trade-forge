"""Example instruments, so a fresh clone has something to backtest against.

Seeds are **not** migrations. A migration describes the shape of the database and
must run exactly once, in order, on every environment including production. Seed
data is convenience: it must be re-runnable, it is dev-only, and it has no business
in the schema's version history. Mixing the two is how a production deploy ends up
inserting four fake symbols.

The numbers below are plausible placeholders. The collector overwrites them with the
truth from MT5 `symbol_info` on the first backfill (PR-102) — through the very same
`upsert_instruments`, which is why the seeds are not a special case with its own
write path that nobody else exercises.

⚠️ Same function, but **not** the same conflict policy, and the difference is the whole
point. "Overwrites them" has to run one way only. Sharing the write path is good; sharing
the authority to overwrite was the bug — see `upsert_instruments(overwrite=...)`.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from tradeforge_db.instruments import upsert_instruments
from tradeforge_engine.domain import AssetClass, InstrumentSpec

INSTRUMENT_SEEDS: tuple[InstrumentSpec, ...] = (
    # Forex: one standard lot is 100 000 units of the base currency, quoted to five
    # decimals. One tick (0.00001) on that lot is worth $1.
    InstrumentSpec(
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
    InstrumentSpec(
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
    InstrumentSpec(
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
    InstrumentSpec(
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


def seed_instruments(session: Session, seeds: tuple[InstrumentSpec, ...] = INSTRUMENT_SEEDS) -> int:
    """Insert the example instruments, never touching one that already exists.

    Safe to re-run; returns how many rows were actually written, which is zero on every run
    after the first. That number reaches the migrate log, so it has to be the truth.

    `overwrite=False` is the whole fix. A seed's job is "make sure these symbols exist so a
    fresh machine can run something" — it is not a source of facts about a market. The
    collector is. Before this, `docker compose up` ran the seed step and handed the
    placeholders authority over every column the collector had measured.
    """
    return upsert_instruments(session, seeds, overwrite=False)
