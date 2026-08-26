"""What this machine was told about itself. Every safeguard's number lives here.

⚠️ **Read on this machine, never from the core.** That is the whole point of `sdd.md` §3.3.3:
a limit that arrives over the network is a limit that stops existing when the network does, and
the moment the core is misbehaving is the moment a limit matters most.

⚠️ **The defaults are the conservative ones**, chosen with Guilherme on 25/08: 2% of the opening
balance, a tenth of a lot, one position at a time, all day. On a demo account the cost of erring
low is a missed trade and the cost of erring high is not, so the asymmetry decides.
"""

import datetime as dt
from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from tradeforge_db.config import PostgresSettings
from tradeforge_executor.safety import Limits

__all__ = ["ExecutorSettings"]


class ExecutorSettings(PostgresSettings):
    """Connection settings plus this machine's own ceilings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    redis_host: str = "localhost"
    redis_port: int = 6379

    kill_switch_file: Path = Path("data/EXECUTOR_KILL")
    """The layer that survives Redis. `touch` engages it, `rm` releases it, from any shell.

    ⚠️ On Windows this is not the fallback — measured 25/08, a process running natively cannot be
    signalled from outside at all, so the file and the Redis key are the *only* handles an
    operator has that do not involve killing the process.
    """

    max_daily_loss_percent: Decimal = Decimal("2")
    max_order_volume: Decimal = Decimal("0.10")
    max_open_positions: int = 1

    window_open: dt.time = dt.time(0, 0)
    window_close: dt.time = dt.time(0, 0)
    """Equal ends mean all day. See `Limits.is_open_at` — the empty window is unreachable by
    construction, so nobody can configure "never" by accident."""

    block_ms: int = Field(default=5_000, gt=0)
    """How long one read waits for an order before coming back empty-handed.

    Shorter than the candle stream's minute, and for the opposite reason: bars arrive on the
    market's schedule and orders arrive on a strategy's, so waiting a minute here would delay a
    stop being moved. It also bounds how long a shutdown takes to be noticed.
    """

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"

    @property
    def limits(self) -> Limits:
        """The ceilings as the safeguards want them.

        ⚠️ Built here rather than stored, so `Limits.__post_init__` validates whatever the
        environment said. A `MAX_OPEN_POSITIONS=0` in a `.env` is a permanent refusal wearing the
        clothes of a limit, and it should fail at start-up with a sentence — not at the first
        order with a refusal that reads like a strategy problem.
        """
        return Limits(
            max_daily_loss_percent=self.max_daily_loss_percent,
            max_volume=self.max_order_volume,
            max_positions=self.max_open_positions,
            window_open=self.window_open,
            window_close=self.window_close,
        )
