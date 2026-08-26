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

from tradeforge_engine.domain import ZERO, Side, SignalKind
from tradeforge_executor.gateway import HeldPosition, OrderGateway, Placement
from tradeforge_executor.safety import AccountSnapshot, KillSwitch, Limits, admits
from tradeforge_executor.wire import Instruction, WireCancel, WireModifyStop

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
        self, order: Instruction, *, now: dt.datetime, core_is_alive: bool = True
    ) -> Outcome:
        """Decide, then act. Returns what to record — recording itself is the caller's.

        ⚠️ **Three kinds arrive on one stream, and the dispatch is here rather than upstream.**
        Order of arrival is the reason they share a stream (see `wire.KIND`): a limit armed and
        cancelled two bars later must be placed before it is withdrawn, and two streams would let
        the executor do it the other way round. Splitting them apart earlier would give that
        ordering back for nothing.

        ⚠️ **A gateway that cannot be read is a refusal, not a crash.** Asking the account is the
        first thing that touches the terminal, and a terminal that is not answering is exactly
        when an order must not go out. Letting the exception escape would abort the loop and
        leave the entry unacknowledged, so the same order would be retried against the same dead
        terminal, for ever.
        """
        held: HeldPosition | None = None
        if isinstance(order, WireModifyStop):
            checked = self._verified(order)
            if isinstance(checked, Outcome):
                return checked
            held = checked

        try:
            account = self._snapshot(now)
        except Exception as error:  # a broken terminal is a refusal with a reason
            logger.exception("could not read the account; refusing %s", order.client_id)
            return self._refused(order, f"the terminal could not be read: {error}")

        # Spelled as a branch rather than two conditional expressions because a `bool` does not
        # narrow a union: mypy reads `order.request` on the cancel arm and is right to.
        if isinstance(order, WireCancel):
            intent, volume = SignalKind.CANCEL, ZERO
        elif isinstance(order, WireModifyStop):
            intent, volume = SignalKind.MODIFY_STOP, ZERO
        else:
            intent, volume = order.request.intent, order.request.volume

        verdict = admits(
            # ⚠️ **The intent, not just the size.** Without it every gate reads an exit as though
            # it were an entry — and with the default `max_positions=1` that meant a session
            # could open a position and never close it. See `safety.admits`.
            intent=intent,
            volume=volume,
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
            placement = self._act(order, held)
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

    def _act(self, order: Instruction, held: HeldPosition | None) -> Placement:
        """Do the one thing this instruction asks for. Nothing here decides anything."""
        if isinstance(order, WireCancel):
            return self.gateway.withdraw(order.client_id)
        if isinstance(order, WireModifyStop):
            # `held` is not None here: `_verified` returned one, and an `Outcome` on every path
            # where it could not. Asserted rather than branched on, because a branch no test can
            # enter is a branch nothing keeps honest.
            assert held is not None, "a verified stop move arrived without a position"  # noqa: S101
            return self.gateway.tighten(held.ticket, order.stop_loss)
        return self.gateway.send(order.request, client_id=order.client_id)

    def _verified(self, order: WireModifyStop) -> HeldPosition | Outcome:
        """The position this move acts on, or the refusal that stops it here.

        ⚠️ **This check is the only one there is.** Measured against a live terminal on 26/08: MT5
        accepts a stop moved *further* from price — `retcode=10009`, and the position comes back
        loosened. The venue has no opinion about the direction; the engine's rule lives three
        processes away in a component this machine does not trust on principle; and the position
        was sized against the stop it already has (`RiskManager`). A sign error upstream is
        therefore one accepted instruction away from risk nobody authorised.

        ⚠️ **The identity is established here, by magic, before anything is sent.** The
        `TRADE_ACTION_SLTP` request carries no magic and no symbol — measured, the terminal echoes
        `magic=0, symbol=''` — so the position ticket is the entire identity of the instruction.
        A ticket that did not come from this filter could belong to a manual trade or to another
        expert advisor.
        """
        try:
            held = self.gateway.held(order.symbol)
        except Exception as error:
            logger.exception("could not read the position for %s", order.client_id)
            return self._refused(order, f"the position could not be read: {error}")

        if held is None:
            return self._refused(order, f"no position of ours in {order.symbol} to protect")

        # A position carrying no stop at all is modifiable in one direction only, and this is it:
        # arming protection where there was none can only reduce risk. `Broker.modify_stop` says
        # the same thing from the other end.
        if held.stop_loss is None:
            return held

        tightens = (
            order.stop_loss >= held.stop_loss
            if held.side is Side.LONG
            else order.stop_loss <= held.stop_loss
        )
        if not tightens:
            logger.error(
                "refusing to loosen the stop of %s %s from %s to %s",
                held.side.value,
                order.symbol,
                held.stop_loss,
                order.stop_loss,
            )
            return self._refused(
                order,
                f"moving the stop of a {held.side.value} {order.symbol} from {held.stop_loss} "
                f"to {order.stop_loss} loosens it: the position was sized against the level it "
                f"already has, and the venue accepts this without objecting",
            )
        return held

    def _snapshot(self, now: dt.datetime) -> AccountSnapshot:
        return AccountSnapshot(
            opening_balance=self.gateway.balance(),
            realised_today=self.gateway.realised_since(start_of_day(now)),
            open_positions=self.gateway.open_positions(),
        )

    def _refused(self, order: Instruction, reason: str) -> Outcome:
        return Outcome(
            client_id=order.client_id,
            session_id=order.session_id,
            allowed=False,
            reason=reason,
            placement=None,
        )
