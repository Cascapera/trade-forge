"""The decision that stands between a strategy and the money.

`specs/fase-3.md` makes this the gate of the whole phase: nothing touches a real account until
the kill switch and these limits are tested. So every rule here is separated on its **boundary**,
not on a comfortable value — a cap tested only with a number twice too large passes against an
implementation that has no cap at all in the neighbourhood that matters.
"""

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal

import pytest

from tradeforge_executor.safety import AccountSnapshot, KillSwitch, Limits, Verdict, admits

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


class Switch:
    """A kill-switch layer that answers what it was told to."""

    def __init__(self, engaged: bool, *, name: str = "test") -> None:
        self._engaged = engaged
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def engaged(self) -> bool:
        return self._engaged


def healthy(
    *,
    opening_balance: Decimal = Decimal("10000"),
    realised_today: Decimal = Decimal("0"),
    open_positions: int = 0,
) -> AccountSnapshot:
    return AccountSnapshot(
        opening_balance=opening_balance,
        realised_today=realised_today,
        open_positions=open_positions,
    )


def ask(  # noqa: PLR0913 — the same six inputs `admits` takes, with defaults
    *,
    volume: Decimal = Decimal("0.10"),
    account: AccountSnapshot | None = None,
    limits: Limits | None = None,
    switches: Sequence[KillSwitch] = (),
    now: dt.datetime = NOON,
    core_is_alive: bool = True,
) -> Verdict:
    """One order, with everything healthy unless a test says otherwise."""
    return admits(
        volume=volume,
        account=account if account is not None else healthy(),
        limits=limits if limits is not None else Limits(),
        switches=switches,
        now=now,
        core_is_alive=core_is_alive,
    )


# --------------------------------------------------------------------------------------------
# The kill switch
# --------------------------------------------------------------------------------------------


def test_an_order_passes_when_nothing_objects() -> None:
    """⚠️ The separating half of every refusal below. Without it, an `admits` that refused
    everything would pass the whole rest of this file."""
    verdict = ask()

    assert verdict.allowed is True
    assert bool(verdict) is True


@pytest.mark.parametrize("engaged_index", [0, 1, 2])
def test_any_single_layer_stops_everything(engaged_index: int) -> None:
    """⚠️ An OR, never an AND. Three layers exist because each survives a different failure, and
    a design that required agreement would be a switch the *loss of a layer* disables.

    Parametrised over the position on purpose: an implementation that only read the first would
    pass a test that only ever engaged the first.
    """
    switches = [Switch(False, name=f"layer-{i}") for i in range(3)]
    switches[engaged_index] = Switch(True, name=f"layer-{engaged_index}")

    verdict = ask(switches=switches)

    assert verdict.allowed is False
    assert f"layer-{engaged_index}" in verdict.reason


def test_the_kill_switch_outranks_every_other_rule() -> None:
    """⚠️ When two rules are broken at once the operator is told about the switch, not the lot
    size. One means somebody pulled the handle and the other means a strategy asked for too
    much — reporting the second while the first is engaged sends a person looking in the wrong
    place."""
    verdict = ask(
        switches=[Switch(True, name="the-handle")],
        volume=Decimal("99"),
        account=healthy(open_positions=50, realised_today=Decimal("-9000")),
        now=NOON,
        limits=Limits(window_open=dt.time(1), window_close=dt.time(2)),
    )

    assert "kill switch" in verdict.reason
    assert "the-handle" in verdict.reason


# --------------------------------------------------------------------------------------------
# The core going quiet
# --------------------------------------------------------------------------------------------


def test_a_silent_core_stops_new_orders() -> None:
    verdict = ask(core_is_alive=False)

    assert verdict.allowed is False
    assert "not answering" in verdict.reason


def test_a_silent_core_does_not_liquidate_anything() -> None:
    """⚠️ Defensive, not liquidating — the decision of 25/08. `admits` answers about **new
    orders** and has no vocabulary for closing a position, which is the design saying it out
    loud: flattening on a silent core turns a sixty-second network fault into a market exit,
    possibly at the bottom of a wick, while the stop already resting at the venue is real
    protection that does not depend on this process at all.

    Pinned structurally, because the alternative would arrive as a *new function*, not as a
    changed answer.
    """
    from tradeforge_executor import safety  # noqa: PLC0415 — the module is the subject

    assert not any("flatten" in name or "liquidate" in name for name in safety.__all__)


# --------------------------------------------------------------------------------------------
# The limits, each on its boundary
# --------------------------------------------------------------------------------------------


def test_a_lot_exactly_at_the_cap_is_allowed_and_one_step_over_is_not() -> None:
    """The boundary, both sides. A cap tested only with a number twice too large passes against
    an implementation with no cap in the neighbourhood that matters."""
    limits = Limits(max_volume=Decimal("0.10"))

    assert ask(volume=Decimal("0.10"), limits=limits).allowed is True
    assert ask(volume=Decimal("0.11"), limits=limits).allowed is False


def test_the_volume_refusal_says_both_numbers() -> None:
    verdict = ask(volume=Decimal("0.50"), limits=Limits(max_volume=Decimal("0.10")))

    assert "0.50" in verdict.reason
    assert "0.10" in verdict.reason


def test_the_position_cap_counts_the_one_being_asked_for() -> None:
    """⚠️ `>=`, not `>`. With a cap of one and one position already open, the order being judged
    would make two — an implementation comparing with `>` allows exactly one too many, and the
    default cap of 1 is precisely where that is invisible."""
    limits = Limits(max_positions=1)

    assert ask(account=healthy(open_positions=0), limits=limits).allowed is True
    assert ask(account=healthy(open_positions=1), limits=limits).allowed is False


def test_a_higher_position_cap_admits_more_than_one() -> None:
    """The separating half: without it, a rule that refused whenever anything was open would
    pass the test above."""
    limits = Limits(max_positions=3)

    assert ask(account=healthy(open_positions=2), limits=limits).allowed is True
    assert ask(account=healthy(open_positions=3), limits=limits).allowed is False


def test_reaching_the_daily_loss_cap_stops_trading() -> None:
    """⚠️ Reaching the cap **is** hitting it. A `>` would let the account lose exactly its limit
    and then take one more trade, which is the one reading of "maximum daily loss" nobody means.
    """
    limits = Limits(max_daily_loss_percent=Decimal("2"))

    just_under = ask(account=healthy(realised_today=Decimal("-199.99")), limits=limits)
    exactly = ask(account=healthy(realised_today=Decimal("-200")), limits=limits)

    assert just_under.allowed is True
    assert exactly.allowed is False, "the cap was reached and a further order was allowed"


def test_the_daily_cap_is_a_share_of_the_balance_not_a_fixed_amount() -> None:
    """⚠️ Separates a percentage from a hard-coded number. On a 1 000 account the same 2% is
    20 — an implementation that compared against a constant would pass every test above, which
    all use a balance of 10 000."""
    limits = Limits(max_daily_loss_percent=Decimal("2"))
    small = healthy(opening_balance=Decimal("1000"), realised_today=Decimal("-20"))

    assert ask(account=small, limits=limits).allowed is False
    assert (
        ask(
            account=healthy(opening_balance=Decimal("1000"), realised_today=Decimal("-19.99")),
            limits=limits,
        ).allowed
        is True
    )


def test_a_profitable_day_is_never_a_loss() -> None:
    """A sign error here would stop trading on the days it is going well."""
    assert ask(account=healthy(realised_today=Decimal("500"))).allowed is True


# --------------------------------------------------------------------------------------------
# The trading window
# --------------------------------------------------------------------------------------------


def test_the_default_window_is_all_day() -> None:
    """Equal ends mean "all day", not "no time at all". The empty window is unreachable by
    construction, and a configuration that produced it would look like a broker that never
    answers rather than like a mistake."""
    for hour in (0, 6, 12, 23):
        assert ask(now=NOON.replace(hour=hour)).allowed is True


def test_an_order_outside_an_ordinary_window_is_refused() -> None:
    limits = Limits(window_open=dt.time(8), window_close=dt.time(17))

    assert ask(now=NOON.replace(hour=9), limits=limits).allowed is True
    assert ask(now=NOON.replace(hour=18), limits=limits).allowed is False
    assert ask(now=NOON.replace(hour=7), limits=limits).allowed is False


def test_a_window_that_crosses_midnight_is_the_normal_case() -> None:
    """⚠️ Forex runs 22:00-22:00 UTC and a session avoiding the Asian open is 06:00-21:00. A
    naive `open <= t < close` refuses the entire night for the first — which is *all* of it."""
    overnight = Limits(window_open=dt.time(22), window_close=dt.time(6))

    assert ask(now=NOON.replace(hour=23), limits=overnight).allowed is True, "22:00-00:00"
    assert ask(now=NOON.replace(hour=2), limits=overnight).allowed is True, "00:00-06:00"
    assert ask(now=NOON.replace(hour=12), limits=overnight).allowed is False, "the middle"


def test_the_window_opens_on_its_first_second_and_closes_on_its_last() -> None:
    """Half-open, and stated: a bar at exactly the close is outside. Otherwise a 08:00-17:00
    window and a 17:00-02:00 window would both admit 17:00, and one of them is wrong."""
    limits = Limits(window_open=dt.time(8), window_close=dt.time(17))

    assert ask(now=NOON.replace(hour=8, minute=0), limits=limits).allowed is True
    assert ask(now=NOON.replace(hour=17, minute=0), limits=limits).allowed is False


def test_a_naive_clock_is_refused() -> None:
    """⚠️ A naive `now` against a UTC window shifts the whole trading day by whatever the
    machine's offset happens to be — and this machine is a Windows box in Brazil talking to a
    broker on UTC+3."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ask(now=dt.datetime(2026, 8, 25, 12))  # noqa: DTZ001


def test_a_clock_in_another_zone_is_converted_not_read_off_the_face() -> None:
    """⚠️ The window is UTC and the caller may not be — this project's broker runs on UTC+3.

    **Both hours here are chosen so the two readings disagree**, which is the whole test. The
    first pick was 20:00 UTC+3 (= 17:00 UTC): outside the window either way, so it passed against
    an implementation reading the wall-clock hour. These do not:

    * 10:00 UTC+3 is 07:00 UTC — *before* the window opens, while the face says 10:00, inside;
    * 19:00 UTC+3 is 16:00 UTC — inside, while the face says 19:00, outside.
    """
    limits = Limits(window_open=dt.time(8), window_close=dt.time(17))
    at_broker = dt.timezone(dt.timedelta(hours=3))

    too_early = dt.datetime(2026, 8, 25, 10, tzinfo=at_broker)
    still_open = dt.datetime(2026, 8, 25, 19, tzinfo=at_broker)

    assert ask(now=too_early, limits=limits).allowed is False, "07:00 UTC is before the open"
    assert ask(now=still_open, limits=limits).allowed is True, "16:00 UTC is inside"


# --------------------------------------------------------------------------------------------
# Limits that make no sense are refused where they are written
# --------------------------------------------------------------------------------------------


def test_a_non_positive_loss_cap_is_refused() -> None:
    for value in (Decimal("0"), Decimal("-1")):
        with pytest.raises(ValueError, match="must be positive"):
            Limits(max_daily_loss_percent=value)


def test_a_non_positive_volume_cap_is_refused() -> None:
    for value in (Decimal("0"), Decimal("-0.1")):
        with pytest.raises(ValueError, match="must be positive"):
            Limits(max_volume=value)


def test_a_position_cap_of_zero_is_refused() -> None:
    """⚠️ Zero would be a permanent refusal wearing the clothes of a limit. Stopping trading is
    the kill switch's job, and the switch says so in the audit log — a cap of zero refuses every
    order with a reason that reads like a strategy problem."""
    with pytest.raises(ValueError, match="at least 1"):
        Limits(max_positions=0)
