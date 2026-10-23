"""Three ways to stop this machine trading, and each one survives what the others do not.

They are an **OR**: any layer engaged stops everything. A design that required agreement would
be a switch that the *loss of a layer* disables, which is the opposite of what a switch is for.

| layer | reachable when | survives |
|---|---|---|
| `RedisFlag` | the core and the UI are up | this process being unreachable from a terminal |
| `FileFlag` | somebody has a shell on this box | Redis being down, the network being gone |
| `EndpointFlag` | the executor's own HTTP port answers | Redis *and* the filesystem misbehaving |

⚠️ **Every layer fails closed.** A layer that cannot read its own state answers `True`. That is
the opposite of ordinary availability engineering and it is right here: refusing an order that
should have been allowed costs a missed trade, and allowing one that should have been refused
costs money. The asymmetry is the whole argument.

⚠️ **On Windows the Redis layer is not redundancy.** Measured 25/08: a process running natively
on Windows cannot be signalled from outside — `taskkill` without `/F` is refused outright and
`kill -INT` never reaches the interpreter. The flag is the only handle an operator has that does
not involve killing the process and leaving a session row to be reconciled.
"""

import logging
import threading
from pathlib import Path
from typing import Protocol

from redis.typing import KeyT, ResponseT

logger = logging.getLogger(__name__)

__all__ = ["ENGAGED_VALUE", "SWITCH_KEY", "EndpointFlag", "FileFlag", "FlagStore", "RedisFlag"]

# The key the UI and the API write to. One key for the whole executor, not one per session: a
# kill switch that has to be aimed is a kill switch somebody aims wrong in a hurry.
SWITCH_KEY = "executor:kill-switch"

# Any value at all engages it, but this is what the writers set. Read as presence, never parsed —
# a switch whose meaning depends on parsing a payload can be disengaged by a malformed one.
ENGAGED_VALUE = "engaged"


class FlagStore(Protocol):
    """The one Redis call this module makes, in redis-py's own vocabulary.

    ⚠️ **Wrong in both directions on the first try, and they are opposite rules.** A protocol is
    satisfied by a *wider* parameter and a *narrower* return — so `names: str` was too narrow
    (the real client takes `KeyT`) and `-> int` was too narrow as well, because `Redis.exists`
    is declared `ResponseT`, which covers the async client too.

    Get either wrong and the protocol describes a client that does not exist: every double
    written against it type-checks while the real `Redis` does not. `CandleStream`'s protocol
    had to learn the same thing from a test; here mypy said it the moment the real client was
    passed in, which is why `process.py` passing it is worth more than any assertion.
    """

    def exists(self, *names: KeyT) -> ResponseT: ...


class RedisFlag:
    """Engaged while the key exists. **Engaged, too, if Redis cannot be asked.**"""

    def __init__(self, store: FlagStore, *, key: str = SWITCH_KEY) -> None:
        self._store = store
        self._key = key

    @property
    def name(self) -> str:
        return f"redis:{self._key}"

    def engaged(self) -> bool:
        try:
            # `bool` of whatever the client answered: 1 or 0 from the sync client, and the
            # protocol admits `ResponseT` so this is the one place that narrows it.
            return bool(self._store.exists(self._key))
        except Exception:
            # ⚠️ Not "assume it is fine". An unreachable Redis is exactly the situation in which
            # somebody may have been trying to engage the switch and failed to be heard.
            logger.exception("could not read the kill switch from Redis; treating it as engaged")
            return True


class FileFlag:
    """Engaged while a file exists on this machine. The layer that survives Redis.

    A path, not a value inside a file: `touch` engages it and `rm` releases it, from any shell,
    with no client and no parsing. ⚠️ A path that cannot be interrogated at all — a permission
    error, a dead mount — engages it, for the same reason `RedisFlag` does.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def name(self) -> str:
        return f"file:{self._path}"

    def engaged(self) -> bool:
        try:
            return self._path.exists()
        except OSError:
            logger.exception("could not stat %s; treating the switch as engaged", self._path)
            return True


class EndpointFlag:
    """Engaged in this process, by whatever is serving the emergency endpoint.

    The last layer, and the one that works when Redis is down *and* the filesystem is unhappy.
    Thread-safe because the HTTP handler that engages it is not the thread that reads it.

    ⚠️ **`engage()` is one-way.** Releasing it is deliberately not offered here: coming back from
    a kill is an operator decision made with a running system in front of them, and an endpoint
    that can un-kill is an endpoint a retry loop can un-kill.
    """

    def __init__(self) -> None:
        self._engaged = threading.Event()

    @property
    def name(self) -> str:
        return "endpoint"

    def engage(self) -> None:
        logger.critical("kill switch engaged through the emergency endpoint")
        self._engaged.set()

    def engaged(self) -> bool:
        return self._engaged.is_set()
