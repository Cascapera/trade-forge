"""`WS /ws/backtests/{id}` — a live feed of one run's progress.

The worker and the API are separate processes, so progress cannot be handed across in memory:
the worker publishes each transition to a Redis channel, and this endpoint subscribes to that
channel and forwards what it hears to the socket. When a terminal event arrives (`done` or
`failed`) the feed has nothing more to say, so it closes.
"""

import contextlib
import json
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from tradeforge_api.queue import progress_channel

router = APIRouter()

_TERMINAL = {"done", "failed"}


@router.websocket("/ws/backtests/{backtest_id}")
async def backtest_progress(websocket: WebSocket, backtest_id: uuid.UUID) -> None:
    await websocket.accept()
    settings = websocket.app.state.settings
    redis: Redis = Redis(host=settings.redis_host, port=settings.redis_port)
    pubsub = redis.pubsub()
    await pubsub.subscribe(progress_channel(backtest_id))
    # Announce that the subscription is live before any progress can arrive. A client (and a
    # test) then knows that from this point on, nothing published to the channel will be missed.
    await websocket.send_text(json.dumps({"status": "subscribed"}))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue  # subscription confirmations and the like are not progress
            data = message["data"]
            text = data.decode() if isinstance(data, bytes) else str(data)
            await websocket.send_text(text)
            if _is_terminal(text):
                break
    except WebSocketDisconnect:
        pass  # the client hung up; unwinding below is all that is left to do
    finally:
        await pubsub.unsubscribe(progress_channel(backtest_id))
        await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis ships no stub for this
        await redis.aclose()
        await _close_quietly(websocket)


def _is_terminal(text: str) -> bool:
    try:
        return json.loads(text).get("status") in _TERMINAL
    except (ValueError, AttributeError):
        return False


async def _close_quietly(websocket: WebSocket) -> None:
    # Already closed by the disconnect is fine; anything else is not ours to swallow.
    with contextlib.suppress(RuntimeError):
        await websocket.close()
