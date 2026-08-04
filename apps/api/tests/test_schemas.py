"""The response shapes, checked where they touch the database's own conventions.

Most of these DTOs are a straight projection of a row and need no test of their own. The
entry snapshot is not: it crosses two boundaries that each have a rule the other does not
know about — JSONB's "empty object means nothing was recorded", and the wire's "money is a
string, never a JSON number". Both are silent when broken. An empty object reaching a client
as a snapshot produces a chart with no bars; a price reaching it as a float produces one that
disagrees in the last place with the trade printed beside it.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_api.schemas import SnapshotOut, TradeOut

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def a_snapshot_payload(*, regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The shape `tradeforge_db.results._snapshot` writes into the column."""
    return {
        "decided_at": "2024-01-01T00:00:00+00:00",
        "filled_at": "2024-01-01T01:00:00+00:00",
        "bars": [
            {
                "time": "2024-01-01T00:00:00+00:00",
                "open": "1.10000",
                "high": "1.10050",
                "low": "1.09950",
                "close": "1.10000",
            },
            {
                "time": "2024-01-01T01:00:00+00:00",
                "open": "1.10100",
                "high": "1.10150",
                "low": "1.10050",
                "close": "1.10100",
            },
        ],
        "regions": regions if regions is not None else [],
    }


def a_trade_payload(*, snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": 1,
        "direction": "long",
        "entry_time": START,
        "entry_price": Decimal("1.10000"),
        "exit_time": START + HOUR,
        "exit_price": Decimal("1.10100"),
        "exit_reason": "tp",
        "volume": Decimal("1"),
        "stop_loss": None,
        "take_profit": None,
        "gross_pnl": Decimal("100"),
        "costs": Decimal("0"),
        "net_pnl": Decimal("100"),
        "r_multiple": None,
        "context": {},
        "snapshot": snapshot,
    }


@pytest.mark.parametrize("empty", [{}, None])
def test_a_trade_with_no_recorded_window_reports_no_snapshot(empty: Any) -> None:
    """`{}` is the column's NOT NULL default: it means "no window was recorded", which is what
    every row written before `rev_0003` says. Reported as a false flag, not as an error — the
    trade is real and its numbers are fine; only the picture is missing."""
    trade = TradeOut.model_validate(a_trade_payload(snapshot=empty))
    assert trade.has_snapshot is False


def test_a_trade_with_a_window_advertises_it_without_carrying_it() -> None:
    """The list says a picture exists; it does not ship one.

    Fifty-odd bars is ~7 kB per trade, and a five-hundred-trade run would assemble megabytes to
    be almost entirely discarded — a reader opens the two or three entries that look wrong. The
    body is asserted to contain no bars at all, because a field that quietly came back would
    restore the cost with nothing failing.
    """
    trade = TradeOut.model_validate(a_trade_payload(snapshot=a_snapshot_payload()))
    assert trade.has_snapshot is True

    body = trade.model_dump(mode="json")
    assert body["has_snapshot"] is True
    assert "snapshot" not in body
    assert "bars" not in json.dumps(body)


def test_the_flag_reads_the_column_and_not_a_field_of_its_own_name() -> None:
    """The alias is the whole mechanism. Without it Pydantic looks for `has_snapshot` on the
    ORM row, does not find it, and returns the default — every trade reporting no picture,
    quietly and always, with the endpoint still answering 200."""
    assert "snapshot" in TradeOut.model_fields["has_snapshot"].validation_alias.choices  # type: ignore[union-attr]


def test_a_snapshot_crosses_the_wire_with_its_prices_as_strings() -> None:
    """The exact-decimal discipline, carried to the last hop. A bar that crossed as a JSON
    number would be an IEEE double, and the chart drawn from it would not line up with the
    `entry_price` printed beside it."""
    snapshot = SnapshotOut.model_validate(a_snapshot_payload())

    body = snapshot.model_dump(mode="json")
    assert [bar["close"] for bar in body["bars"]] == ["1.10000", "1.10100"]
    assert all(isinstance(bar["high"], str) for bar in body["bars"])


def test_the_entry_bar_is_findable_without_knowing_the_list_is_ordered() -> None:
    """`filled_at` is what a chart marks the entry on. It is written out rather than left to be
    read off the last element, so a client never has to assume an ordering to draw a marker."""
    snapshot = SnapshotOut.model_validate(a_snapshot_payload())
    assert snapshot.filled_at == snapshot.bars[-1].time
    assert snapshot.decided_at == snapshot.bars[0].time


def test_a_region_keeps_a_left_edge_older_than_the_window() -> None:
    """Routinely older: the zone was formed by a candle the arming window may not reach back to.
    A client that clamped `from_time` into the visible range would redraw the zone as younger
    than it is, which is the one thing the rectangle exists to show."""
    region = {
        "label": "zone",
        "top": "1.10200",
        "bottom": "1.10000",
        "from_time": "2023-12-31T19:00:00+00:00",
    }
    snapshot = SnapshotOut.model_validate(a_snapshot_payload(regions=[region]))

    (drawn,) = snapshot.regions
    assert drawn.label == "zone"
    assert drawn.top == Decimal("1.10200")
    assert drawn.from_time < snapshot.bars[0].time

    body = snapshot.model_dump(mode="json")["regions"][0]
    assert body["top"] == "1.10200"
    assert body["bottom"] == "1.10000"


def test_regions_default_to_empty_rather_than_missing() -> None:
    """A setup with no zone — the swing ones enter off an average — writes no regions. The
    client should iterate an empty list, not branch on a key that may not be there."""
    payload = a_snapshot_payload()
    del payload["regions"]
    assert SnapshotOut.model_validate(payload).regions == []
