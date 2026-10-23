"""Recording a share split, against a real Postgres.

Run locally with:  POSTGRES_DB=tradeforge_test uv run pytest -m integration
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.models import Instrument, StockSplit
from tradeforge_db.splits import RecordedSplit, StockMarket, record_split, splits_of, stock_markets
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

THREE_HOURS = dt.timedelta(hours=3)


def a_share(session: Session, symbol: str = "AMZN") -> Instrument:
    instrument = Instrument(
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.STOCK,
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal(1),
        digits=2,
        server_offset=THREE_HOURS,
    )
    session.add(instrument)
    session.flush()
    return instrument


def test_a_recorded_split_reads_back_with_its_broker_s_clock(session: Session) -> None:
    a_share(session)

    recorded = record_split(session, "AMZN", dt.date(2022, 6, 6), shares_after=20, shares_before=1)
    session.commit()

    expected = RecordedSplit("AMZN", dt.date(2022, 6, 6), 20, 1, THREE_HOURS)
    assert recorded == expected
    assert splits_of(session, "AMZN") == (expected,)


def test_splits_come_back_oldest_first_and_only_the_share_s_own(session: Session) -> None:
    a_share(session, "TSLA")
    a_share(session, "AAPL")
    record_split(session, "TSLA", dt.date(2022, 8, 25), shares_after=3, shares_before=1)
    record_split(session, "TSLA", dt.date(2020, 8, 31), shares_after=5, shares_before=1)
    record_split(session, "AAPL", dt.date(2020, 8, 31), shares_after=4, shares_before=1)

    days = [split.effective_on for split in splits_of(session, "TSLA")]

    assert days == [dt.date(2020, 8, 31), dt.date(2022, 8, 25)]
    assert splits_of(session, "NVDA") == ()


def test_the_same_day_twice_is_refused_in_words(session: Session) -> None:
    """Recorded twice, it would be applied twice: a history divided by 400."""
    a_share(session)
    record_split(session, "AMZN", dt.date(2022, 6, 6), shares_after=20, shares_before=1)
    session.commit()

    with pytest.raises(ValueError, match="already has a split on 2022-06-06"):
        record_split(session, "AMZN", dt.date(2022, 6, 6), shares_after=20, shares_before=1)


def test_a_split_for_a_share_nobody_catalogued_is_refused(session: Session) -> None:
    with pytest.raises(LookupError, match="no instrument called ZZZZ"):
        record_split(session, "ZZZZ", dt.date(2022, 6, 6), shares_after=2, shares_before=1)


@pytest.mark.parametrize(("after", "before"), [(0, 1), (2, 0), (3, 3)])
def test_the_database_refuses_a_ratio_that_is_not_a_split(
    session: Session, after: int, before: int
) -> None:
    instrument = a_share(session)
    session.add(
        StockSplit(
            instrument_id=instrument.id,
            effective_on=dt.date(2022, 6, 6),
            shares_after=after,
            shares_before=before,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()


def test_only_shares_are_searched_for_splits(session: Session) -> None:
    a_share(session, "NVDA")
    session.add(
        Instrument(
            symbol="EURUSD",
            name="Euro",
            asset_class=AssetClass.FOREX,
            currency_base="EUR",
            currency_quote="USD",
            tick_size=Decimal("0.00001"),
            tick_value=Decimal(1),
            contract_size=Decimal(100000),
            digits=5,
        )
    )
    session.flush()

    assert stock_markets(session) == [StockMarket("NVDA", THREE_HOURS)]
