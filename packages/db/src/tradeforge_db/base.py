"""The declarative base, the constraint naming convention, and the column types.

Two decisions live here, and both are load-bearing for the rest of the project.

**Names.** Postgres will happily invent a name for an unnamed constraint
(`strategies_name_version_key`). Alembic then cannot write a `downgrade()` that
drops it, because the name it must drop was never written down anywhere. The
naming convention below makes every index, unique, check and foreign key name a
pure function of its columns — so a migration can always undo itself.

**Money is never a float.** `0.1 + 0.2 != 0.3` in binary floating point. A
backtest sums thousands of fills; that error compounds until the equity curve
disagrees with the broker statement, and no amount of test coverage on the engine
would explain why. `NUMERIC` is exact decimal arithmetic: slower, and worth it.
"""

from sqlalchemy import MetaData, Numeric
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# A price. Ten decimal places covers 5-digit forex (0.00001), JPY crosses, and
# crypto, with room left over.
PRICE = Numeric(precision=20, scale=10)

# Money: P&L, capital, costs. Eight places is far beyond any currency's minor
# unit — the excess exists so that intermediate results never need rounding.
MONEY = Numeric(precision=20, scale=8)

# Lots or units.
VOLUME = Numeric(precision=18, scale=8)

# Dimensionless: win rate, profit factor, Sharpe, R-multiple. Numeric rather than
# float here too, so that a metric read back from the database is bit-for-bit the
# number that was computed — a backtest that reports two different Sharpes on two
# reads is not reproducible, and reproducibility is the whole point.
RATIO = Numeric(precision=18, scale=8)


class Base(DeclarativeBase):
    """Declarative base for every table in the system."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
