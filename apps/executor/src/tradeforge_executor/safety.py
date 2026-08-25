"""Whether an order is allowed to leave this machine. No MT5, no queue, no database.

**This module is the reason the phase has a mandatory order.** `specs/fase-3.md` says nothing
touches a real account until the kill switch and the executor's limits are tested, and the only
way to test them properly is for the decision to be reachable without a broker, a network or a
clock that ticks. So the deciding is here and the doing is in `process.py`.

Two doctrines run through all of it.

**Any layer that says stop, stops.** The kill switch is an OR, never an AND. Three layers exist
because each survives a different failure — the Redis flag when the process is unreachable, the
local file when Redis is, the in-process endpoint when neither is — and a design that required
agreement between them would be a kill switch that the *loss of a layer* disables.

⚠️ **Fail-safe, not fail-open.** A layer that cannot be read counts as **engaged**. This is the
opposite of what most availability engineering teaches, and it is right here for one reason: the
cost of refusing an order that should have been allowed is a missed trade, and the cost of
allowing one that should have been refused is money. A kill switch that fails open is not a kill
switch; it is a comment.

⚠️ **Windows makes the Redis layer load-bearing, not redundant.** Measured on 25/08: a session
running natively on Windows cannot be signalled from another process — `taskkill` without `/F` is
refused outright and `kill -INT` never reaches the interpreter. The flag is the only layer an
operator can reach from outside.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "AccountSnapshot",
    "KillSwitch",
    "Limits",
    "Verdict",
    "admits",
]


@dataclass(frozen=True, slots=True)
class Verdict:
    """Allowed, or refused with the rule that refused it.

    ⚠️ The reason is not decoration. Every refusal is written to `order_audit`, and "refused"
    without which rule leaves an operator unable to tell a kill switch from a lot that was one
    step too large — two situations with opposite responses.
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOWED = Verdict(allowed=True)


def _refused(reason: str) -> Verdict:
    return Verdict(allowed=False, reason=reason)


class KillSwitch(Protocol):
    """One layer of the switch. `engaged()` must answer, never raise.

    A layer that cannot determine its own state answers `True` — see the module docstring. It is
    the implementation's job to turn its own errors into that answer, because a layer that raised
    would take the decision out of `admits` and into an exception handler somewhere, which is
    exactly where a fail-open bug hides.
    """

    @property
    def name(self) -> str: ...

    def engaged(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Limits:
    """The executor's own ceilings, independent of anything the core believes.

    ⚠️ **Local on purpose** (`sdd.md` §3.3.3). A limit that lives in the core is a limit that
    stops existing when the core does — and the moment the core is misbehaving is the moment a
    limit matters most. These are read from the environment on this machine and enforced here.
    """

    max_daily_loss_percent: Decimal = Decimal("2")
    """Refuse once the day's realised loss reaches this share of the day's opening balance."""

    max_volume: Decimal = Decimal("0.10")
    """The largest lot a single order may carry."""

    max_positions: int = 1
    """How many positions may be open at once, counting the one being asked for."""

    window_open: dt.time = dt.time(0, 0)
    window_close: dt.time = dt.time(0, 0)
    """The trading window, in **UTC**. Equal ends mean "all day", not "no time at all" — the
    empty window is unreachable by construction and a configuration that produced it would look
    like a broker that never answers rather than like a mistake."""

    def __post_init__(self) -> None:
        if self.max_daily_loss_percent <= 0:
            raise ValueError(f"daily loss cap must be positive, got {self.max_daily_loss_percent}")
        if self.max_volume <= 0:
            raise ValueError(f"volume cap must be positive, got {self.max_volume}")
        if self.max_positions < 1:
            # Zero would be a permanent refusal wearing the clothes of a limit. Stopping trading
            # is the kill switch's job, and it says so in the audit log.
            raise ValueError(f"position cap must be at least 1, got {self.max_positions}")

    def is_open_at(self, moment: dt.time) -> bool:
        """⚠️ Windows that cross midnight are the normal case, not the exception. Forex runs
        22:00-22:00 UTC and a session that avoids the Asian open is 06:00-21:00; comparing with
        a naive `open <= t <= close` refuses the whole night for the first and the whole day for
        the second."""
        if self.window_open == self.window_close:
            return True
        if self.window_open < self.window_close:
            return self.window_open <= moment < self.window_close
        return moment >= self.window_open or moment < self.window_close


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """What the executor believes about the account right now.

    Passed in rather than read here, so the decision stays testable without a terminal. The
    caller is the one talking to MT5, and the one that knows whether the number is fresh.
    """

    opening_balance: Decimal
    """The balance the day started with. The denominator of the daily loss cap."""

    realised_today: Decimal
    """Today's closed P&L. Negative is a loss."""

    open_positions: int
    """How many positions this executor is holding, by its own magic number."""


def admits(  # noqa: PLR0913, PLR0911 — one guard per rule; each returns the reason it refused
    *,
    volume: Decimal,
    account: AccountSnapshot,
    limits: Limits,
    switches: Sequence[KillSwitch],
    now: dt.datetime,
    core_is_alive: bool = True,
) -> Verdict:
    """May this order leave the machine?

    ⚠️ **The order of the checks is the order of the reasons**, and it is deliberate. When two
    rules are broken at once the operator is told about the kill switch, not about the lot size:
    one of them means somebody pulled the handle and the other means a strategy asked for too
    much, and reporting the second while the first is engaged would send a person looking in the
    wrong place.
    """
    if now.tzinfo is None:
        # ⚠️ A naive `now` against a UTC window silently shifts the whole trading day by
        # whatever the machine's offset happens to be — and this machine is a Windows box in
        # Brazil talking to a broker on UTC+3. Refused rather than assumed, the same way
        # `Candle.time` refuses one.
        raise ValueError("now must be timezone-aware; a naive clock cannot be UTC")

    for switch in switches:
        if switch.engaged():
            return _refused(f"kill switch engaged ({switch.name})")

    if not core_is_alive:
        # ⚠️ Defensive, not liquidating. Open positions keep the stop they already have at the
        # venue, which is real protection that does not depend on this process. Flattening on a
        # silent core would turn a sixty-second network fault into a market exit, possibly at
        # the bottom of a wick — a cure that reproduces the disease.
        return _refused("the core is not answering; no new orders while it is silent")

    if not limits.is_open_at(now.astimezone(dt.UTC).time()):
        return _refused(
            f"outside the trading window "
            f"({limits.window_open:%H:%M}-{limits.window_close:%H:%M} UTC)"
        )

    if volume > limits.max_volume:
        return _refused(f"volume {volume} is above the cap of {limits.max_volume}")

    if account.open_positions >= limits.max_positions:
        return _refused(
            f"{account.open_positions} position(s) already open, cap is {limits.max_positions}"
        )

    loss_cap = account.opening_balance * limits.max_daily_loss_percent / Decimal(100)
    if -account.realised_today >= loss_cap:
        # ⚠️ `>=`, and the boundary matters: reaching the cap *is* hitting it. A `>` would let
        # the account lose exactly its limit and then take one more trade, which is the one
        # reading of "maximum daily loss" that nobody means.
        return _refused(
            f"today's loss of {-account.realised_today} has reached the cap of {loss_cap}"
        )

    return ALLOWED
