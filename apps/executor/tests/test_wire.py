"""The envelope: three instructions, one stream, one order of arrival.

No Redis and no terminal — this is the format, and the format is a pure function. What it has to
get right is that an entry it cannot read comes back as an error rather than as a plausible
order, because the dead-letter path is walked by the service and the plausible order is walked by
the account.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import OrderRequest, Side, SignalKind
from tradeforge_executor.wire import (
    KIND,
    WireCancel,
    WireModifyStop,
    WireOrder,
    cancel_fields,
    instruction_from_fields,
    modify_stop_fields,
    order_fields,
)

NOON = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)


def an_order() -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal("0.10"),
        decided_at=NOON,
        stop_loss=Decimal("1.09500"),
    )


def test_an_order_survives_the_round_trip_with_its_kind() -> None:
    fields = order_fields(an_order(), session_id="s-1", client_id="zone-42")

    assert fields[KIND] == "order"
    instruction = instruction_from_fields(fields)

    assert isinstance(instruction, WireOrder)
    assert instruction.request.volume == Decimal("0.10")


def test_a_cancel_survives_the_round_trip() -> None:
    instruction = instruction_from_fields(cancel_fields(session_id="s-1", client_id="zone-42"))

    assert isinstance(instruction, WireCancel)
    assert instruction.client_id == "zone-42"


def test_a_stop_move_survives_the_round_trip_with_its_level_and_its_instant() -> None:
    """The level as decimal text and the instant as ISO-8601. Both are evidence in
    `order_audit.request`, and a level that went through a float is evidence about a different
    number."""
    instruction = instruction_from_fields(
        modify_stop_fields(
            session_id="s-1",
            client_id="s-1-4",
            symbol="EURUSD",
            stop_loss=Decimal("1.16500"),
            decided_at=NOON,
        )
    )

    assert isinstance(instruction, WireModifyStop)
    assert str(instruction.stop_loss) == "1.16500"
    assert instruction.decided_at == NOON


def test_an_entry_with_no_kind_is_refused_rather_than_read_as_an_order() -> None:
    """⚠️ **A `fields.get(KIND, KIND_ORDER)` would be the whole bug.**

    An entry this format cannot read is not "probably an order". Defaulted, a malformed or
    older entry becomes an order — sent to the venue, filled, and recorded as fine — and the
    session spends the day holding a position nobody asked for. A `KeyError` is a message on the
    dead-letter path, which the service already knows how to walk.

    Same doctrine as `order_from_fields` refusing to default a missing `volume` to zero.
    """
    fields = order_fields(an_order(), session_id="s-1", client_id="zone-42")
    del fields[KIND]

    with pytest.raises(KeyError):
        instruction_from_fields(fields)


def test_an_unknown_kind_is_refused_rather_than_read_as_an_order() -> None:
    """The other half, and the one a *newer* writer produces: an executor running yesterday's
    code meets an instruction invented today. Refusing names the kind it could not read; falling
    back to `order` would execute the fields it happens to recognise."""
    fields = order_fields(an_order(), session_id="s-1", client_id="zone-42")
    fields[KIND] = "liquidate_everything"

    with pytest.raises(ValueError, match="unknown instruction kind"):
        instruction_from_fields(fields)


def test_the_kind_is_the_only_field_that_decides() -> None:
    """A cancel carrying an order's leftover fields is still a cancel. The dispatch reads `kind`
    and nothing else, so an entry cannot be half one thing and half another depending on which
    field a reader happened to look at first."""
    fields = order_fields(an_order(), session_id="s-1", client_id="zone-42")
    fields[KIND] = "cancel"

    assert isinstance(instruction_from_fields(fields), WireCancel)
