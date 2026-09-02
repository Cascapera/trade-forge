"""Turning one `venue.outcomes` entry into one panel event.

The socket itself needs a server; this does not. What lives here is the decision the socket makes
about *every* entry that passes under it — and the stream is fan-out, so most of them belong to
somebody else.
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from tradeforge_api.ws import _event_for
from tradeforge_executor.wire import (
    WireFill,
    WireRefusal,
    fill_fields,
    refusal_fields,
)

MINE = uuid.UUID("11111111-1111-1111-1111-111111111111")
YOURS = uuid.UUID("22222222-2222-2222-2222-222222222222")
AT = dt.datetime(2026, 9, 1, 14, 30, tzinfo=dt.UTC)


def _fill(session_id: uuid.UUID, *, client_id: str = "zone-1") -> dict[str, str]:
    return fill_fields(
        WireFill(
            client_id=client_id,
            session_id=str(session_id),
            symbol="EURUSD",
            at=AT,
            price=Decimal("1.10500"),
            volume=Decimal("0.10"),
            spread=Decimal("0.00002"),
            ticket=987654,
        )
    )


def _refusal(session_id: uuid.UUID, *, by_venue: bool, retcode: int | None) -> dict[str, str]:
    return refusal_fields(
        WireRefusal(
            client_id="zone-2",
            session_id=str(session_id),
            at=AT,
            reason="kill switch engaged (redis:executor:kill-switch)",
            by_venue=by_venue,
            retcode=retcode,
        )
    )


def test_another_sessions_outcome_is_not_this_sessions_event() -> None:
    """⚠️ **The one that matters, because `venue.outcomes` is fan-out.**

    Every outcome any executor publishes for any session passes under this reader. A panel that
    forwarded all of them would show one strategy's fills on another strategy's screen — and it
    would look completely normal, because a fill is a fill.
    """
    assert _event_for(_fill(MINE), MINE) is not None
    assert _event_for(_fill(YOURS), MINE) is None


def test_a_fill_carries_the_three_numbers_as_text() -> None:
    """Money crosses this socket as text for the same reason it crosses the HTTP wire as text: a
    JSON number is an IEEE double, and the exact-decimal discipline would end here."""
    event = _event_for(_fill(MINE), MINE)

    assert event == {
        "type": "fill",
        "client_id": "zone-1",
        "at": AT.isoformat(),
        "symbol": "EURUSD",
        "price": "1.10500",
        "volume": "0.10",
        "spread": "0.00002",
    }


def test_a_refusal_says_who_refused() -> None:
    """⚠️ `by_venue` separates two refusals that read alike and behave oppositely.

    Ours describe conditions that change on their own; the terminal's usually do not. A panel
    showing one word for both tells somebody to wait when they should be fixing something.
    """
    ours = _event_for(_refusal(MINE, by_venue=False, retcode=None), MINE)
    theirs = _event_for(_refusal(MINE, by_venue=True, retcode=10027), MINE)

    assert ours is not None
    assert theirs is not None
    assert ours["by_venue"] is False
    assert theirs["by_venue"] is True


def test_a_refusal_that_never_reached_the_venue_has_no_retcode() -> None:
    """⚠️ Absent and zero are different facts, and only one of them is a verdict.

    A refusal our own safeguards made never asked MetaTrader anything, so there is no number to
    report. Reporting `0` would be inventing an answer from a venue that was never consulted.
    """
    event = _event_for(_refusal(MINE, by_venue=False, retcode=None), MINE)

    assert event is not None
    assert event["retcode"] is None


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({}, id="empty"),
        pytest.param({"kind": "something-else", "session_id": str(MINE)}, id="unknown kind"),
        pytest.param({"session_id": str(MINE), "client_id": "x"}, id="no kind at all"),
    ],
)
def test_an_unreadable_entry_is_skipped_rather_than_fatal(fields: dict[str, str]) -> None:
    """⚠️ **Skipped, never raised**, and the reason is the fan-out again.

    Anything any producer writes to this stream passes under this reader. Letting one unreadable
    entry raise would close a panel watching a perfectly healthy session, for a reason that has
    nothing to do with that session — and the operator would see the screen die at the exact
    moment they were using it to work out what was wrong.
    """
    assert _event_for(fields, MINE) is None


def test_a_fill_missing_its_spread_is_skipped_and_not_priced_at_zero() -> None:
    """The wire refuses a fill that cannot say what crossing cost, and so does this.

    ⚠️ Defaulting the spread to zero would put a free trade on the screen. `fill_from_fields`
    raises for exactly that reason; the socket's job is only to not die of it.
    """
    fields = _fill(MINE)
    del fields["spread"]

    assert _event_for(fields, MINE) is None
