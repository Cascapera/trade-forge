"""`/executor/kill-switch` — the handle that stops the executor taking on new risk.

⚠️ **The only endpoint in this API with a consequence measured in money**, and it is the one
that *removes* a consequence. Everything else here catalogues, queues and reports; this writes
the key `tradeforge_executor.safety.admits` consults before every order that would open a
position.

**Why a Redis key and not a job on the queue.** The queue is how this API asks for everything
else, and it is exactly wrong here: the queue is drained by the process the button exists to
stop. A stuck executor would leave the job waiting for ever and the button would do nothing at
the one moment it is pressed. The key is read with `EXISTS`, per order, by a code path that does
not depend on the order loop being healthy.

**Why there is no `DELETE`.** Engaging is one-way from here, by decision (31/08): coming back
from a kill is an operator decision made with a running system in front of them, and an endpoint
that can un-kill is an endpoint a retry can un-kill. Release is `redis-cli DEL
executor:kill-switch` — a shell, on purpose.

⚠️ **Unauthenticated, like every other route here, and that is only defensible because of the
line above.** With no release path, the whole abuse surface of this endpoint is "somebody stops
trading who should not have". The switch fails safe and so does its API; add a `DELETE` and that
argument evaporates along with it.
"""

import datetime as dt

from fastapi import APIRouter, HTTPException, status
from redis.exceptions import RedisError

from tradeforge_api.deps import KillSwitchDep
from tradeforge_api.kill_switch import LAYER, SwitchState
from tradeforge_api.schemas import KillSwitchOut

router = APIRouter(tags=["executor"])

_UNREACHABLE = (
    "could not reach the kill switch in Redis. The executor treats an unreadable Redis as "
    "engaged, but do not rely on that from here: engage the file layer on the executor's own "
    "machine (touch its kill_switch_file) if trading must stop now."
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _out(state: SwitchState) -> KillSwitchOut:
    return KillSwitchOut(engaged=state.engaged, engaged_at=state.engaged_at, layer=LAYER)


@router.post("/executor/kill-switch", response_model=KillSwitchOut)
def engage(switch: KillSwitchDep) -> KillSwitchOut:
    """Engage the Redis layer of the kill switch. Idempotent.

    `200`, not `202`: the write is done when this returns, and there is no job to follow. What
    is *not* instantaneous is the effect — the executor reads the flag when it next decides an
    order, so an order already in flight at `order_send` completes. That is a bounded window of
    one order, and closing it would mean cancelling at the venue, which is a different mechanism
    and a different PR.

    ⚠️ **This does not close positions and does not stop the session.** Exits, cancels and
    tightening stops pass a raised switch by design, so the strategy can still get out; entries
    are refused. A screen that labels this "encerrar tudo" is describing something the system
    does not do.

    ⚠️ **It is not free to leave engaged while a session runs.** Every arming attempt becomes a
    refusal, and three refusals in a row retire that zone for the rest of the session
    (`setups.MAX_ARMING_ATTEMPTS`) — releasing the switch does not bring it back. Same declared
    cost as any other outage longer than three bars.
    """
    try:
        return _out(switch.engage(now=_utcnow()))
    except RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{_UNREACHABLE} ({error})"
        ) from error


@router.get("/executor/kill-switch", response_model=KillSwitchOut)
def read(switch: KillSwitchDep) -> KillSwitchOut:
    """What the Redis layer says, and only what it says.

    ⚠️ **503 rather than `engaged: false` when Redis cannot be read**, and the asymmetry with
    `RedisFlag` is deliberate rather than an oversight. That class *decides*, so it fails closed
    and calls an unreadable Redis engaged. This one *reports*, and the safe report of a question
    that could not be asked is not a verdict — it is a refusal to give one. An API answering
    `false` here would be telling an operator the machine is live at the exact moment the
    executor is refusing every order it is handed.
    """
    try:
        return _out(switch.state())
    except RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{_UNREACHABLE} ({error})"
        ) from error
