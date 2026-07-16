"""The progress WebSocket, against real Redis.

The worker and the API talk over a Redis pub/sub channel. Here the endpoint subscribes, and
the test plays the worker's part — publishing a progress event with a plain Redis client — to
prove the endpoint forwards what it hears and closes on a terminal event.

The subscription is confirmed by the endpoint's first frame, so there is no race: the test
publishes only after it has seen `subscribed`, and a message published to a live subscription
is never dropped.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import json
import uuid
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import progress_channel

pytestmark = pytest.mark.integration


class _FakeQueue:
    async def enqueue_job(self, *args: Any) -> None:
        return None


def test_the_socket_forwards_progress_and_closes_on_a_terminal_event() -> None:
    settings = Settings()
    # The WS never touches the database, so the session factory is left to the lifespan (a lazy,
    # never-dialled engine). Only the queue is faked, to keep a real arq pool out of the test.
    app = create_app(settings=settings, arq_pool=_FakeQueue())
    backtest_id = uuid.uuid4()
    publisher = redis.Redis(host=settings.redis_host, port=settings.redis_port)

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/backtests/{backtest_id}") as socket:
            assert socket.receive_json()["status"] == "subscribed"

            channel = progress_channel(backtest_id)
            publisher.publish(channel, json.dumps({"status": "running", "progress": 0.0}))
            assert socket.receive_json() == {"status": "running", "progress": 0.0}

            publisher.publish(channel, json.dumps({"status": "done", "progress": 1.0}))
            assert socket.receive_json() == {"status": "done", "progress": 1.0}
            # `done` is terminal: the endpoint closes, so the next receive raises.
            with pytest.raises(Exception):  # noqa: B017, PT011 — a disconnect of some flavour
                socket.receive_json()

    publisher.close()
