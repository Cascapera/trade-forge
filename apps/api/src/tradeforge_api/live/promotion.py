"""Whether a strategy has earned the right to trade real money.

Two rules guard that, in two places, and the split is the point of this module existing at all.

**The database holds the invariant.** `rev_0016` refuses to insert a `live` session for a
strategy that has never completed a bar in paper — a trigger, applying to everyone, undoable only
by a migration. It is not a number anybody should be able to argue with at three in the morning.

**This holds the policy.** *How many* days is a judgement that moves with confidence, and
`specs/fase-3.md` calls it configurable. A number that needs a migration to change is not
configuration; it is a schema decision wearing configuration's clothes.

⚠️ **The count is per strategy, not per plan**, and that is worth knowing before reading a live
session as evidence. Paper trading a strategy on EURUSD M15 unlocks it on GBPUSD H1 — the
operator's deliberate choice, made with the trade-off in front of them. What the gate attests is
that *this strategy has been watched running*; the spread, liquidity and noise it was watched
against are not necessarily the ones it will meet. See `rev_0016`.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_db.models import LiveSession, SessionMode

__all__ = ["Promotion", "paper_days", "promotion_for"]


@dataclass(frozen=True, slots=True)
class Promotion:
    """Whether this strategy may go live, and the arithmetic behind the answer."""

    allowed: bool
    days: int
    required: int
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def paper_days(session: Session, strategy_id: uuid.UUID) -> int:
    """How many distinct days this strategy has processed a bar in paper.

    ⚠️ **Distinct UTC dates, not sessions.** A strategy restarted six times on one Tuesday has a
    single day of evidence, and counting rows instead would make the gate a measure of how often
    somebody pressed start.

    ⚠️ **`last_bar_time IS NOT NULL`, not `started_at`.** A session that opened and died saw no
    market. Without this the gate is defeated by a shell loop, which is not a hypothetical way to
    cheat so much as the exact shape an impatient afternoon takes.

    ⚠️ **The date comes from `last_bar_time`, not from `started_at`.** They differ for a session
    that runs across midnight, and the bar's own instant is the honest one: it says which day's
    market this strategy was actually watched trading, which is the thing being counted.
    """
    dates = select(func.date(LiveSession.last_bar_time)).where(
        LiveSession.strategy_id == strategy_id,
        LiveSession.mode == SessionMode.PAPER,
        LiveSession.last_bar_time.is_not(None),
    )
    counted = select(func.count()).select_from(dates.distinct().subquery())
    return int(session.execute(counted).scalar_one())


def promotion_for(
    session: Session, strategy_id: uuid.UUID, *, mode: SessionMode, required_days: int
) -> Promotion:
    """May a session for this strategy run in `mode`?

    Paper is always allowed and the database is not consulted for it — a gate that made paper
    trading conditional on anything would be a gate standing in front of the very thing it exists
    to require.
    """
    if mode is not SessionMode.LIVE:
        return Promotion(allowed=True, days=0, required=0)

    # ⚠️ Refused rather than treated as "no requirement". A configuration of zero would mean
    # every strategy is promoted the moment it exists, which is not a looser policy — it is the
    # absence of one, arriving through the field that was supposed to express it. The database's
    # floor would still hold, so the account is not exposed; what would be lost is the operator's
    # ability to see that they had switched the policy off.
    if required_days < 1:
        return Promotion(
            allowed=False,
            days=0,
            required=required_days,
            reason=(
                f"live_promotion_days is {required_days}: a promotion gate that requires nothing "
                f"is not a configured gate, it is a disabled one, and disabling it is a decision "
                f"that should look like one"
            ),
        )

    days = paper_days(session, strategy_id)
    if days >= required_days:
        return Promotion(allowed=True, days=days, required=required_days)
    return Promotion(
        allowed=False,
        days=days,
        required=required_days,
        reason=(
            f"strategy {strategy_id} has {days} day(s) of paper trading on record and "
            f"{required_days} are required; paper it for {required_days - days} more trading "
            f"day(s) before risking an account on it"
        ),
    )
