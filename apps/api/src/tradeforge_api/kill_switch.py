"""Writing the one kill-switch layer this process can reach — and the honest limits of that.

The executor's `tradeforge_executor.kill_switch` describes three layers and reads all three.
This module is the **writer** for exactly one of them, the Redis key, because that is the only
one an API in a Linux container can touch: the file layer lives on the executor's own disk and
the endpoint layer lives in the executor's own memory.

⚠️ **The key name is imported, never spelled again.** `SWITCH_KEY` is the executor's constant.
A copy here would agree on the day it was written and disagree on the day one side changed —
and the disagreement would be silent in the worst possible place: the button would report
success while writing to a key nobody reads. The API already depends on `tradeforge-executor`
for the order wire, for the same reason.

⚠️ **This process cannot answer "is the executor stopped?".** It can only answer "is *this*
layer engaged?". An operator who ran `touch data/EXECUTOR_KILL` on the executor's box has
stopped it in a way nothing here can see, so every answer below names the layer it observed
rather than claiming to speak for the switch as a whole.

⚠️ **Engaging is not flattening.** `safety.admits` clears exits, cancels and tightening stops
*before* it consults the switches, deliberately — a switch that refused an exit would be an
operator pulling the handle and staying in the trade. So this stops new risk and leaves the
session able to get out; positions keep the venue-side stop they already have.
"""

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from redis.typing import EncodableT, KeyT, ResponseT

from tradeforge_executor.kill_switch import ENGAGED_VALUE, SWITCH_KEY

logger = logging.getLogger(__name__)

__all__ = ["ENGAGED_AT_KEY", "LAYER", "KillSwitch", "SwitchState", "SwitchStore"]

ENGAGED_AT_KEY = f"{SWITCH_KEY}:engaged-at"
"""When the switch was engaged, as ISO-8601 text, in a key of its own.

⚠️ **Beside the switch, never inside it.** The executor reads `SWITCH_KEY` as *presence* and
says why in as many words: a switch whose meaning depends on parsing a payload is a switch a
malformed payload disengages. So the audit stamp is a sibling key the executor never looks at,
and nothing about whether trading stops depends on this string being readable, or present, or
even well-formed.

⚠️ Releasing from a shell is still `DEL executor:kill-switch` alone. A stamp left behind is
inert: it is only ever reported while the switch itself is engaged, and re-engaging overwrites
it.
"""

LAYER = f"redis:{SWITCH_KEY}"
"""What to call this layer out loud — and it is `RedisFlag.name`, character for character.

The executor stamps that same string into the refusal it writes to `order_audit`
(*"kill switch engaged (redis:executor:kill-switch)"*). One vocabulary, so the screen that
engaged it and the audit row that explains a refused order are visibly the same fact.
"""


class SwitchStore(Protocol):
    """The three Redis calls this module makes, in redis-py's own vocabulary.

    ⚠️ **Spelled from the real client's signature, not from what a fake happens to need.** A
    protocol is satisfied by a *wider* parameter and a *narrower* return, so both halves are
    easy to get backwards — `FlagStore` next door carries the scars, and this project has since
    watched the same mistake arrive a third way, through positional order. The proof is not this
    docstring: it is `main._lifespan` handing a real `Redis` to `KillSwitch`, which is the line
    that makes mypy check the claim.
    """

    def exists(self, *names: KeyT) -> ResponseT: ...

    def get(self, name: KeyT) -> ResponseT: ...

    def mset(self, mapping: Mapping[str, EncodableT]) -> ResponseT: ...


@dataclass(frozen=True, slots=True)
class SwitchState:
    """What one look at the Redis layer found."""

    engaged: bool

    engaged_at: dt.datetime | None
    """When it was engaged, or `None` for "engaged, and nobody recorded when".

    ⚠️ Not a default of "now" and not the epoch. The stamp can genuinely be missing — a switch
    engaged with `redis-cli SET` by hand has no stamp at all — and a screen that invented a time
    would be showing an operator a fact nobody established.
    """


@dataclass(frozen=True, slots=True)
class KillSwitch:
    """Engage the Redis layer, and read back what is there.

    ⚠️ **No `release`, by decision.** Coming back from a kill is an operator decision made with
    a running system in front of them, and an endpoint that can un-kill is an endpoint a retry
    loop can un-kill — `EndpointFlag` makes the same argument about the layer it owns. Releasing
    is `redis-cli DEL executor:kill-switch`, from a shell, by somebody who has looked.
    """

    store: SwitchStore

    def state(self) -> SwitchState:
        """What the Redis layer says right now.

        ⚠️ **Raises rather than guessing** when Redis cannot be reached. `RedisFlag` answers
        `True` in that situation, because it is *deciding* and the safe decision is to refuse;
        this is *reporting*, and the safe report is not a verdict at all. A screen told
        `engaged: false` by an API that could not reach Redis would be contradicting the
        executor, which is at that very moment refusing everything.
        """
        engaged = bool(self.store.exists(SWITCH_KEY))
        return SwitchState(engaged=engaged, engaged_at=self._engaged_at() if engaged else None)

    def engage(self, *, now: dt.datetime) -> SwitchState:
        """Engage the layer. Idempotent, and the second press does not rewrite the first's time.

        ⚠️ **One `mset`, not two `set`s.** The two keys go in a single atomic call, so there is
        no window in which the switch is engaged with a stamp still missing, or — far worse in
        the other order — a stamp written for a switch that never got engaged.

        ⚠️ **The `exists` first is about the audit, not the switch.** Re-pressing a button that
        is already down must not overwrite when it was first pressed; that timestamp is the only
        fact this unauthenticated surface can record about who did what. Two presses racing at
        the same millisecond can still let the second win the stamp, which costs nothing anybody
        can measure — the alternative is a compare-and-set on an emergency path, and complexity
        on this path is a worse bug than a millisecond of ambiguity.
        """
        if bool(self.store.exists(SWITCH_KEY)):
            return self.state()

        logger.critical("kill switch engaged through the API at %s", now.isoformat())
        self.store.mset({SWITCH_KEY: ENGAGED_VALUE, ENGAGED_AT_KEY: now.isoformat()})
        return SwitchState(engaged=True, engaged_at=now)

    def _engaged_at(self) -> dt.datetime | None:
        """The stamp, or `None` if it is absent or unreadable.

        ⚠️ **A bad stamp is never an error.** This is the one place the module parses anything,
        and the parse decides a line of text on a screen — never whether trading stops. Letting
        a hand-written key raise here would turn decoration into an outage, on the endpoint that
        exists because things are already going wrong.
        """
        raw = self.store.get(ENGAGED_AT_KEY)
        text = raw.decode() if isinstance(raw, bytes) else raw
        if not isinstance(text, str):
            return None
        try:
            return dt.datetime.fromisoformat(text)
        except ValueError:
            logger.warning("%s holds %r, which is not a timestamp", ENGAGED_AT_KEY, text)
            return None
