"""One order, from the queue to the venue — or to a refusal, which is the interesting half.

Read the shape of `route_one` and the whole design is there:

    read the account  ->  ask the safeguards  ->  record the answer
                                              \\-> and only if allowed, send

**The audit write happens on every path**, and that ordering is the point of the module. A
refusal that reaches no venue and leaves no record is indistinguishable from an order nobody
ever placed — and "why did my strategy stop trading at 11am" is exactly the question this table
exists to answer.

⚠️ **The safeguards are asked before the account is risked, not after.** Obvious written down,
easy to lose: a loop that sent first and checked the result would have the kill switch stopping
the *next* order rather than this one.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from tradeforge_executor.gateway import OrderGateway, Placement
from tradeforge_executor.safety import AccountSnapshot, KillSwitch, Limits, admits
from tradeforge_executor.wire import WireOrder

logger = logging.getLogger(__name__)

__all__ = ["Outcome", "Router", "start_of_day"]


def start_of_day(now: dt.datetime) -> dt.datetime:
    """Midnight UTC of `now`'s day — the instant the daily loss cap counts from.

    ⚠️ UTC, not the broker's day and not the machine's. A cap that resets on a different clock
    from the one the window is written in is a cap that resets in the middle of a session, and
    the person who set it to 2% would not be able to say when it starts.
    """
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened to one order, in the vocabulary `order_audit` stores."""

    client_id: str
    session_id: str
    allowed: bool
    reason: str | None
    placement: Placement | None

    @property
    def sent(self) -> bool:
        return self.placement is not None and self.placement.accepted


@dataclass(slots=True)
class Router:
    """The safeguards, the venue and the clock, wired together for one decision at a time."""

    gateway: OrderGateway
    limits: Limits
    switches: Sequence[KillSwitch]

    def route_one(
        self, order: WireOrder, *, now: dt.datetime, core_is_alive: bool = True
    ) -> Outcome:
        """Decide, then act. Returns what to record — recording itself is the caller's.

        ⚠️ **A gateway that cannot be read is a refusal, not a crash.** Asking the account is the
        first thing that touches the terminal, and a terminal that is not answering is exactly
        when an order must not go out. Letting the exception escape would abort the loop and
        leave the entry unacknowledged, so the same order would be retried against the same dead
        terminal, for ever.
        """
        try:
            account = self._snapshot(now)
        except Exception as error:  # a broken terminal is a refusal with a reason
            logger.exception("could not read the account; refusing %s", order.client_id)
            return self._refused(order, f"the terminal could not be read: {error}")

        verdict = admits(
            # ⚠️ **The intent, not just the size.** Without it every gate reads an exit as though
            # it were an entry — and with the default `max_positions=1` that meant a session
            # could open a position and never close it. See `safety.admits`.
            intent=order.request.intent,
            volume=order.request.volume,
            account=account,
            limits=self.limits,
            switches=self.switches,
            now=now,
            core_is_alive=core_is_alive,
        )
        if not verdict:
            logger.warning("refused %s: %s", order.client_id, verdict.reason)
            return self._refused(order, verdict.reason)

        try:
            placement = self.gateway.send(order.request, client_id=order.client_id)
        except Exception as error:
            # ⚠️ An exception from the venue is an *outcome*, recorded as one. The alternative is
            # a loop that dies on the first timeout, and an order whose fate nobody wrote down.
            logger.exception("the venue refused %s", order.client_id)
            return self._refused(order, f"the venue could not be reached: {error}")

        return Outcome(
            client_id=order.client_id,
            session_id=order.session_id,
            allowed=True,
            reason=None,
            placement=placement,
        )

    def _snapshot(self, now: dt.datetime) -> AccountSnapshot:
        return AccountSnapshot(
            opening_balance=self.gateway.balance(),
            realised_today=self.gateway.realised_since(start_of_day(now)),
            open_positions=self.gateway.open_positions(),
        )

    def _refused(self, order: WireOrder, reason: str) -> Outcome:
        return Outcome(
            client_id=order.client_id,
            session_id=order.session_id,
            allowed=False,
            reason=reason,
            placement=None,
        )
