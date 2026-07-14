"""The seed table, checked as data — no database required.

Splitting the *data* from the *writer* is what makes this possible: `INSTRUMENT_SEEDS`
is a tuple of frozen dataclasses, and `seed_instruments` is the only part that needs a
session. The numbers below are the ones every example backtest will price against, so
a typo here is a wrong P&L everywhere.
"""

from decimal import Decimal

import pytest

from tradeforge_db.models import AssetClass
from tradeforge_db.seeds import INSTRUMENT_SEEDS, InstrumentSeed


def test_symbols_are_unique() -> None:
    symbols = [seed.symbol for seed in INSTRUMENT_SEEDS]

    assert len(symbols) == len(set(symbols))


def test_more_than_one_asset_class_is_represented() -> None:
    """The point of the seeds is to exercise the multi-asset arithmetic, not just forex.

    A stock's tick is worth a cent per share; a forex tick on a standard lot is worth a
    dollar. If every seed were a currency pair, the first stock backtest would be the
    first time that code path ever ran.
    """
    assert len({seed.asset_class for seed in INSTRUMENT_SEEDS}) >= 3


@pytest.mark.parametrize("seed", INSTRUMENT_SEEDS, ids=lambda seed: seed.symbol)
def test_seed_survives_the_check_constraints(seed: InstrumentSeed) -> None:
    """Every value the database would reject, asserted here — where the message is clearer."""
    assert seed.tick_size > 0
    assert seed.tick_value > 0
    assert seed.contract_size > 0
    assert 0 <= seed.digits <= 10


@pytest.mark.parametrize("seed", INSTRUMENT_SEEDS, ids=lambda seed: seed.symbol)
def test_digits_agree_with_tick_size(seed: InstrumentSeed) -> None:
    """`digits` and `tick_size` are two statements of the same fact, and must agree.

    Five digits means the smallest move is 0.00001. If they disagree, price rounding and
    stop placement disagree with each other — and the backtest fills at prices the market
    never printed.
    """
    assert seed.tick_size == Decimal(1).scaleb(-seed.digits)


@pytest.mark.parametrize("seed", INSTRUMENT_SEEDS, ids=lambda seed: seed.symbol)
def test_only_forex_has_a_base_currency(seed: InstrumentSeed) -> None:
    """A stock settles in one currency. A pair is a ratio between two."""
    if seed.asset_class is AssetClass.FOREX:
        assert seed.currency_base is not None
    else:
        assert seed.currency_base is None
