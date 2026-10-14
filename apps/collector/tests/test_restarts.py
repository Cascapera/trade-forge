"""The restart requests the collector writes, in the hash the supervisor reads (14/10)."""

import datetime as dt
import uuid

from tradeforge_collector.restarts import RESTARTS_KEY, request_restart

WIN = uuid.UUID("c3acc215-3158-492e-8263-df0e662b6429")
AT = dt.datetime(2026, 10, 14, 18, 18, tzinfo=dt.UTC)


class FakeHashes:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hset(self, name: str, key: str, value: str) -> int:
        self.hashes.setdefault(name, {})[key] = value
        return 1


def test_a_request_is_the_markets_field_valued_with_when_it_was_asked() -> None:
    redis = FakeHashes()

    request_restart(redis, WIN, "M5", at=AT)
    request_restart(redis, WIN, "M15", at=AT)

    assert redis.hashes == {
        RESTARTS_KEY: {f"{WIN}:M5": AT.isoformat(), f"{WIN}:M15": AT.isoformat()}
    }
