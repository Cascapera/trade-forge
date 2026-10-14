"""Asking the signals supervisor to restart a market's sessions.

The screen asks when somebody clicks restart; since 14/10 the live loop asks too, once it has
collected again a series its broker rescaled (`live.run_on_demand`, `rescale`): the sessions
warmed on the old scale, and only warming again on the new one mends them.

Here rather than in the API's `fleet`, which reads the requests, because the collector writes them
too and an app does not import the API — the same place `alive.ALIVE_KEY` lives for the same
reason.
"""

import datetime as dt
import uuid
from typing import Protocol

__all__ = ["RESTARTS_KEY", "request_restart", "restart_field"]

RESTARTS_KEY = "signals:restarts"
"""Restart requests: one hash, a field per market (`restart_field`) valued with when it was asked.

⚠️ **One key, not one key per request (13/10).** Requests used to be keys of their own, found each
round with a SCAN of the whole keyspace — instant while Redis held a few thousand keys, 68 s once
a sweep's 2.3 million jobs were back in it: every round took over a minute, and the supervisor's
word lapsed in the middle of each, so the screen read it as stopped."""


class _Hashes(Protocol):
    def hset(self, name: str, key: str, value: str) -> object: ...


def restart_field(instrument_id: uuid.UUID, timeframe: str) -> str:
    """A market's field in `RESTARTS_KEY`."""
    return f"{instrument_id}:{timeframe}"


def request_restart(
    client: _Hashes, instrument_id: uuid.UUID, timeframe: str, *, at: dt.datetime
) -> None:
    """Ask for this market's sessions to be restarted together, as the screen's button does."""
    client.hset(RESTARTS_KEY, restart_field(instrument_id, timeframe), at.isoformat())
