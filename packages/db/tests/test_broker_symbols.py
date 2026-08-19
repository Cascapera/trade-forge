"""The one decision in the snapshot writer that is not a database call.

Everything else in `broker_symbols.py` is SQL and is proved against a real Postgres in
`test_broker_symbols_integration.py`. The refusal below is a *policy*, it fires before the
session is touched, and it is the difference between a bad sync being survivable and a bad sync
taking the screen down — so it is tested here, where it runs on every push with no Docker.
"""

import datetime as dt
from typing import cast

import pytest
from sqlalchemy.orm import Session

from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot

SYNCED_AT = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


class _ExplodingSession:
    """A session that fails if it is used at all.

    ⚠️ The assertion is not only "it raised" but "it raised **before writing**". A guard that
    deleted the old snapshot and then complained would have already done the damage it exists
    to prevent, and a test that only checked the exception type could not tell the two apart.
    """

    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("the snapshot was touched before the guard ran")

    def add_all(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the snapshot was touched before the guard ran")

    def flush(self) -> None:
        raise AssertionError("the snapshot was touched before the guard ran")


def test_an_empty_catalogue_is_refused_before_anything_is_deleted() -> None:
    """⚠️ A terminal that lists no symbols is not a broker with nothing to offer.

    It is a terminal that is not logged in yet, or a `symbols_get` that failed — and both are
    *expected* states, not rare ones. Believing the empty answer would replace a working
    catalogue with nothing, so the search box would go dark whenever a sync happened to run at
    a bad moment, and the only way back is to notice and re-sync by hand.
    """
    with pytest.raises(ValueError, match="not logged in"):
        replace_snapshot(
            cast("Session", _ExplodingSession()),
            [],
            server="MetaQuotes-Demo",
            synced_at=SYNCED_AT,
        )


def test_one_symbol_is_enough_to_be_a_real_catalogue() -> None:
    """The guard is about *nothing*, not about *few*.

    ⚠️ A broker with a single instrument is unusual and legitimate, and a threshold — "refuse
    fewer than five" — would be this project inventing a fact about somebody's account. The
    line is drawn where the meaning changes, and it changes at zero.
    """
    entry = BrokerSymbolEntry(symbol="EURUSD")

    # No exception: it gets past the guard and on to the session, which is what raises here.
    with pytest.raises(AssertionError, match="before the guard ran"):
        replace_snapshot(
            cast("Session", _ExplodingSession()),
            [entry],
            server=None,
            synced_at=SYNCED_AT,
        )


def test_a_symbol_carries_only_what_a_listing_needs() -> None:
    """⚠️ No tick size, no tick value, no asset class — and that is the design, not an omission.

    Filling those in means deciding the symbol's asset class, and the collector deliberately
    refuses to guess it (`asset_class_from_path` returns `None` rather than picking). A
    catalogue listing must not need that decision: its whole job is to show symbols *before*
    anyone has committed to collecting any of them.
    """
    entry = BrokerSymbolEntry(symbol="EURUSD")

    assert entry.description is None
    assert entry.path is None
    assert entry.digits is None
    assert entry.visible is False
    assert not hasattr(entry, "tick_size")
