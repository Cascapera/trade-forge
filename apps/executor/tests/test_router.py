"""One order from the queue to the venue — with no terminal, no queue and no clock that ticks.

The point of the split this file tests: the code that *can lose money* is one small class
(`MT5Gateway`), and every decision that leads to calling it is here, runnable on Linux in CI.

⚠️ The fake gateway is not a stand-in for MT5 and must not be read as one. It answers the four
questions `OrderGateway` asks and nothing else — what a real terminal does with a request is
`test_gateway_smoke.py`'s business, against a real terminal. A fake that pretended to be MT5
would agree with correct code and with wrong code alike, which this project has already paid for.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_engine.domain import OrderRequest, Side, SignalKind
from tradeforge_executor.gateway import Placement
from tradeforge_executor.router import Outcome, Router, start_of_day
from tradeforge_executor.safety import KillSwitch, Limits
from tradeforge_executor.wire import WireOrder, order_fields, order_from_fields

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def an_order(**overrides: Any) -> WireOrder:
    values: dict[str, Any] = {
        "symbol": "EURUSD",
        "side": Side.LONG,
        "intent": SignalKind.ENTRY,
        "volume": Decimal("0.10"),
        "decided_at": NOON,
        "stop_loss": Decimal("1.09500"),
    }
    values.update(overrides)
    return WireOrder(client_id="zone-42", session_id="s-1", request=OrderRequest(**values))


def a_placement(*, accepted: bool = True, volume: str = "0.10") -> Placement:
    return Placement(
        accepted=accepted,
        ticket=99 if accepted else None,
        filled_volume=Decimal(volume),
        price=Decimal("1.10000") if accepted else None,
        retcode=10009 if accepted else 10013,
        comment="done" if accepted else "invalid request",
        raw={"retcode": 10009 if accepted else 10013},
    )


class FakeGateway:
    """The four questions `OrderGateway` asks, and a record of what it was told to send."""

    def __init__(
        self,
        *,
        balance: str = "10000",
        realised: str = "0",
        positions: int = 0,
        placement: Placement | None = None,
        broken: str | None = None,
    ) -> None:
        self._balance = Decimal(balance)
        self._realised = Decimal(realised)
        self._positions = positions
        self._placement = placement or a_placement()
        self._broken = broken
        self.sent: list[tuple[OrderRequest, str]] = []
        self.asked_since: dt.datetime | None = None

    def send(self, order: OrderRequest, *, client_id: str) -> Placement:
        if self._broken == "send":
            raise ConnectionError("the terminal went away mid-order")
        self.sent.append((order, client_id))
        return self._placement

    def balance(self) -> Decimal:
        if self._broken == "account":
            raise ConnectionError("no account")
        return self._balance

    def open_positions(self) -> int:
        return self._positions

    def realised_since(self, moment: dt.datetime) -> Decimal:
        self.asked_since = moment
        return self._realised


class Switch:
    def __init__(self, engaged: bool, *, name: str = "test") -> None:
        self._engaged = engaged
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def engaged(self) -> bool:
        return self._engaged


def route(  # noqa: PLR0913 — the router's inputs, with defaults
    gateway: FakeGateway,
    *,
    order: WireOrder | None = None,
    limits: Limits | None = None,
    switches: Sequence[KillSwitch] = (),
    now: dt.datetime = NOON,
    core_is_alive: bool = True,
) -> Outcome:
    router = Router(gateway=gateway, limits=limits or Limits(), switches=switches)
    return router.route_one(order or an_order(), now=now, core_is_alive=core_is_alive)


# --------------------------------------------------------------------------------------------
# The happy path, and the one thing that must never happen on any other
# --------------------------------------------------------------------------------------------


def test_an_admitted_order_reaches_the_venue() -> None:
    gateway = FakeGateway()

    outcome = route(gateway)

    assert outcome.allowed is True
    assert outcome.sent is True
    assert len(gateway.sent) == 1
    assert gateway.sent[0][1] == "zone-42", "the client id did not travel with the order"


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("kill switch", {"switches": [Switch(True, name="handle")]}),
        ("silent core", {"core_is_alive": False}),
        ("volume cap", {"limits": Limits(max_volume=Decimal("0.01"))}),
        ("window", {"limits": Limits(window_open=dt.time(1), window_close=dt.time(2))}),
    ],
)
def test_a_refused_order_never_reaches_the_venue(label: str, kwargs: Any) -> None:
    """⚠️ **The property that matters more than any other in this file.** A safeguard that
    refuses *after* the order went out is not a safeguard; it is a log entry. Asserted on the
    gateway's own record rather than on the verdict, because the verdict is what the refusal
    already says and the question here is whether anything moved.
    """
    gateway = FakeGateway()

    outcome = route(gateway, **kwargs)

    assert outcome.allowed is False, label
    assert gateway.sent == [], f"{label}: the order was sent anyway"
    assert outcome.placement is None


def test_a_refusal_carries_the_rule_that_refused_it() -> None:
    outcome = route(FakeGateway(), switches=[Switch(True, name="the-handle")])

    assert outcome.reason is not None
    assert "the-handle" in outcome.reason


# --------------------------------------------------------------------------------------------
# A terminal that is not answering
# --------------------------------------------------------------------------------------------


def test_an_account_that_cannot_be_read_is_a_refusal_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ Reading the account is the first thing that touches the terminal, and a terminal that
    is not answering is exactly when an order must not go out.

    Letting the exception escape would abort the loop and leave the entry unacknowledged — so
    the same order would be retried against the same dead terminal, for ever.
    """
    gateway = FakeGateway(broken="account")

    with caplog.at_level("CRITICAL"):
        outcome = route(gateway)

    assert outcome.allowed is False
    assert outcome.reason is not None
    assert "terminal could not be read" in outcome.reason
    assert gateway.sent == []


def test_a_venue_that_throws_mid_order_is_an_outcome_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⚠️ Recorded, not raised. The alternative is a loop that dies on the first timeout and an
    order whose fate nobody wrote down — which is the one thing `order_audit` exists to prevent.
    """
    gateway = FakeGateway(broken="send")

    with caplog.at_level("CRITICAL"):
        outcome = route(gateway)

    assert outcome.allowed is False
    assert outcome.reason is not None
    assert "venue could not be reached" in outcome.reason


def test_an_order_the_venue_rejects_is_reported_as_not_sent() -> None:
    """⚠️ Separates "we sent it" from "it worked". `allowed` is this machine's answer and
    `sent` is the venue's, and collapsing them would file a broker rejection as a safeguard
    refusal — sending an investigation to the wrong machine."""
    gateway = FakeGateway(placement=a_placement(accepted=False))

    outcome = route(gateway)

    assert outcome.allowed is True, "our own rules did admit it"
    assert outcome.sent is False, "but the venue did not take it"
    assert outcome.placement is not None
    assert outcome.placement.retcode == 10013


# --------------------------------------------------------------------------------------------
# The account snapshot the safeguards judge
# --------------------------------------------------------------------------------------------


def test_the_daily_loss_is_counted_from_midnight_utc() -> None:
    """⚠️ UTC, not the broker's day and not the machine's. A cap that resets on a different clock
    from the one the window is written in resets in the middle of a session, and the person who
    set it to 2% could not say when it starts."""
    gateway = FakeGateway()

    route(gateway, now=dt.datetime(2026, 8, 25, 23, 30, tzinfo=dt.UTC))

    assert gateway.asked_since == dt.datetime(2026, 8, 25, 0, 0, tzinfo=dt.UTC)


def test_the_day_starts_at_utc_midnight_even_for_a_clock_in_another_zone() -> None:
    """01:00 on the 26th at UTC+3 is 22:00 on the **25th** in UTC — so the day that matters
    started at midnight on the 25th. Reading the wall-clock date would reset the cap an hour
    early and let a bad night start again."""
    at_broker = dt.datetime(2026, 8, 26, 1, tzinfo=dt.timezone(dt.timedelta(hours=3)))

    assert start_of_day(at_broker) == dt.datetime(2026, 8, 25, 0, 0, tzinfo=dt.UTC)


def test_the_position_count_the_safeguards_see_comes_from_the_venue() -> None:
    """Not from a counter this process keeps. A count kept in memory is a count that is wrong
    after a restart, and after a restart is exactly when it matters."""
    gateway = FakeGateway(positions=1)

    outcome = route(gateway, limits=Limits(max_positions=1))

    assert outcome.allowed is False
    assert outcome.reason is not None
    assert "already open" in outcome.reason


def test_the_loss_cap_uses_the_balance_the_venue_reports() -> None:
    """2% of 1 000 is 20. An implementation with a hard-coded denominator would admit this."""
    gateway = FakeGateway(balance="1000", realised="-20")

    assert route(gateway).allowed is False


# --------------------------------------------------------------------------------------------
# What crosses the wire
# --------------------------------------------------------------------------------------------


def test_an_order_survives_the_round_trip_unchanged() -> None:
    """⚠️ The encoding and the decoding are inverse, and the prices come back as the same
    `Decimal` they went in as. A float round-trip at the edge would give back exactly the
    precision the engine runs in `Decimal` to keep."""
    order = an_order(
        take_profit=Decimal("1.11000"), limit_price=Decimal("1.09800"), reason="zone-42 armed"
    )

    fields = order_fields(order.request, session_id=order.session_id, client_id=order.client_id)
    back = order_from_fields(fields)

    assert back == order
    assert str(back.request.limit_price) == "1.09800", "the quantum changed"


def test_an_absent_price_is_absent_from_the_wire_not_empty() -> None:
    """⚠️ Redis has no NULL. Written as `""`, the empty string would be the only thing telling
    "no take profit" from "a take profit of nothing" — and `Decimal("")` raises where `None`
    reads."""
    order = an_order(take_profit=None, limit_price=None)

    fields = order_fields(order.request, session_id="s-1", client_id="c-1")

    assert "take_profit" not in fields
    assert "limit_price" not in fields
    assert order_from_fields(fields).request.take_profit is None


def test_a_malformed_entry_raises_rather_than_defaulting() -> None:
    """⚠️ A `fields.get("volume", "0")` would turn a malformed entry into an order for nothing —
    sent successfully, recorded as fine — and the session would spend the day believing it had a
    position. A `KeyError` is a message on a dead-letter path; a zero-volume order is a lie."""
    fields = order_fields(an_order().request, session_id="s-1", client_id="c-1")
    del fields["volume"]

    with pytest.raises(KeyError, match="volume"):
        order_from_fields(fields)


def test_every_field_on_the_wire_is_a_string() -> None:
    """A stream entry has no other shape. A `Decimal` handed to `xadd` is an error at the socket,
    and a `float` is a wrong number that never raises."""
    fields = order_fields(an_order().request, session_id="s-1", client_id="c-1")

    assert all(isinstance(value, str) for value in fields.values())
