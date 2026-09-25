"""A cluster: several runs' trades replayed on ONE account — shared capital, portfolio limits, and
the combined drawdown — with no database, no candles on disk and no HTTP.

His ask (25/09), option A of the two weighed that day: the engine runs one strategy on one market
with one position at a time (`loop._open_position`, ADR-0019), and teaching it several on one
account is weeks of work in its most invariant-heavy code. Every member has already been run on
its own and kept its trades; what a cluster adds is what sharing one balance changes:

* **Each trade is sized on the shared account.** Risk = the member's risk % of the account's
  balance at the entry, and the trade then makes `r_multiple` times that — R is net profit over
  the risk the stop defined, after costs and swap, and costs are linear in size, so R carries over
  to any size.
* **The portfolio's limits decide what is taken.** At most `max_open_positions` open at once, and
  the risk of everything open at most `max_open_risk_pct` of the balance. A trade that would break
  either is skipped and counted, never silently dropped.
* **The drawdown is marked, not only booked.** Between entry and exit an open trade is valued at
  each close of its member's own bars (`Mark`), from its entry price and stop — so two positions
  losing at once show as the combined fall they were, not as two separate booked losses.

⚠️ **Sized on the balance, not on the marked equity.** A member alone never holds two positions,
so at each of its entries its equity *is* its balance; the balance is that rule carried over to a
shared account. Sizing on marked equity would let one member's open profit enlarge another's
next trade before it is realised.

⚠️ **Same instant: exits, then marks, then entries**, and between members, the members' order. A
position closed on a bar frees its slot and its risk for an entry on that bar, the way a real
account would see it — the stored times cannot say which came first inside the bar, so the rule
is fixed rather than left to a sort.

What it cannot see, and the engine alone could: members interacting — netting two positions on one
market, opposite signals, one member's exit closing another's. None of them exists in the engine
either (ADR-0019).
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

_ZERO = Decimal(0)


class Skip(StrEnum):
    """Why a member's trade was not taken on the shared account."""

    POSITIONS = "positions"
    """`max_open_positions` were already open."""
    RISK = "risk"
    """Its risk on top of what was open would pass `max_open_risk_pct` of the balance."""
    NO_STOP = "no_stop"
    """It had no stop, so no R to size it by."""
    EMPTY = "empty"
    """The account had nothing left to risk."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_open_positions: int
    max_open_risk_pct: Decimal
    """A fraction: 0.05 is 5% of the balance at risk across everything open."""


@dataclass(frozen=True, slots=True)
class MemberTrade:
    """One of a member's own trades, as its run kept it."""

    member: int
    """The member's place in the cluster's list — also the tie-break between members."""
    entry_time: dt.datetime
    exit_time: dt.datetime
    long: bool
    entry_price: Decimal
    stop: Decimal | None
    r: Decimal | None


@dataclass(frozen=True, slots=True)
class Mark:
    """A close of one member's bars: when, and at what price."""

    member: int
    time: dt.datetime
    close: Decimal


@dataclass(slots=True)
class MemberOutcome:
    offered: int = 0
    taken: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    net_pnl: Decimal = _ZERO


@dataclass(frozen=True, slots=True)
class Point:
    time: dt.datetime
    balance: Decimal
    equity: Decimal


@dataclass(frozen=True, slots=True)
class Replay:
    """What the shared account went through."""

    initial_capital: Decimal
    final_balance: Decimal
    max_drawdown_pct: Decimal
    """Deepest fall of the marked equity from its running peak, as a fraction of that peak."""
    max_drawdown_abs: Decimal
    most_open: int
    """The most positions open at once."""
    members: list[MemberOutcome]
    curve: list[Point]
    """Every change of the account, in order — the caller thins it for storage."""


@dataclass(slots=True)
class _Open:
    trade: MemberTrade
    risk: Decimal
    """Money at risk, fixed at the entry."""
    distance: Decimal
    """|entry - stop|: what one R is in price."""
    last: Decimal
    """The latest marked price."""

    def unrealised(self) -> Decimal:
        move = self.last - self.trade.entry_price
        return self.risk * (move if self.trade.long else -move) / self.distance


class _Account:
    """The shared account as the timeline walks it: balance, open positions, the curve."""

    def __init__(self, capital: Decimal, risk_pcts: Sequence[Decimal], limits: Limits) -> None:
        self.balance = capital
        self.risk_pcts = risk_pcts
        self.limits = limits
        self.outcomes = [MemberOutcome() for _ in risk_pcts]
        self.open: dict[int, _Open] = {}  # keyed by the trade's place in the input, unique
        self.exits: list[tuple[dt.datetime, int]] = []
        self.curve: list[Point] = []
        self.most = 0
        self.peak = capital
        self.deepest_abs = _ZERO
        self.deepest_pct = _ZERO

    def equity(self) -> Decimal:
        return self.balance + sum((one.unrealised() for one in self.open.values()), _ZERO)

    def record(self, when: dt.datetime) -> None:
        now = self.equity()
        self.curve.append(Point(when, self.balance, now))
        self.peak = max(self.peak, now)
        fall = self.peak - now
        self.deepest_abs = max(self.deepest_abs, fall)
        if self.peak > 0:
            self.deepest_pct = max(self.deepest_pct, fall / self.peak)

    def close_until(self, when: dt.datetime) -> None:
        """Book every exit at or before `when` — at `when` too: exits lead a tie."""
        self.exits.sort()
        while self.exits and self.exits[0][0] <= when:
            left, key = self.exits.pop(0)
            held = self.open.pop(key)
            pnl = (held.trade.r or _ZERO) * held.risk
            self.balance += pnl
            self.outcomes[held.trade.member].net_pnl += pnl
            self.record(left)

    def mark(self, mark: Mark) -> None:
        touched = False
        for held in self.open.values():
            if held.trade.member == mark.member:
                held.last = mark.close
                touched = True
        if touched:
            self.record(mark.time)

    def why_not(self, trade: MemberTrade) -> Skip | None:
        if trade.stop is None or trade.r is None or trade.stop == trade.entry_price:
            return Skip.NO_STOP
        if self.balance <= 0:
            return Skip.EMPTY
        if len(self.open) >= self.limits.max_open_positions:
            return Skip.POSITIONS
        held = sum((one.risk for one in self.open.values()), _ZERO)
        if held + self.balance * self.risk_pcts[trade.member] > (
            self.limits.max_open_risk_pct * self.balance
        ):
            return Skip.RISK
        return None

    def enter(self, key: int, trade: MemberTrade) -> None:
        outcome = self.outcomes[trade.member]
        outcome.offered += 1
        skip = self.why_not(trade)
        if skip is not None:
            outcome.skipped[skip.value] = outcome.skipped.get(skip.value, 0) + 1
            return
        assert trade.stop is not None  # noqa: S101 — `why_not` refused a trade without one
        outcome.taken += 1
        self.open[key] = _Open(
            trade=trade,
            risk=self.balance * self.risk_pcts[trade.member],
            distance=abs(trade.entry_price - trade.stop),
            last=trade.entry_price,
        )
        self.exits.append((trade.exit_time, key))
        self.most = max(self.most, len(self.open))
        self.record(trade.entry_time)


# Order of kinds at one instant: exits are booked before anything else (`close_until`), then the
# marks, then the entries — so a slot freed on a bar can be taken on that bar.
_MARK, _ENTRY = 0, 1


def replay(
    *,
    initial_capital: Decimal,
    risk_pcts: Sequence[Decimal],
    trades: Sequence[MemberTrade],
    marks: Sequence[Mark],
    limits: Limits,
) -> Replay:
    """Replay `trades` on one account of `initial_capital`, sizing member `i`'s at `risk_pcts[i]`
    (a fraction) of the balance, under `limits`, marking open positions at `marks`."""
    events: list[tuple[dt.datetime, int, int, int, Mark | MemberTrade]] = [
        (trade.entry_time, _ENTRY, trade.member, key, trade) for key, trade in enumerate(trades)
    ]
    events += [(mark.time, _MARK, mark.member, key, mark) for key, mark in enumerate(marks)]
    events.sort(key=lambda event: event[:4])

    account = _Account(initial_capital, risk_pcts, limits)
    if events:
        account.record(events[0][0])
    for when, _kind, _member, key, item in events:
        account.close_until(when)
        if isinstance(item, Mark):
            account.mark(item)
        else:
            account.enter(key, item)
    if account.exits:
        account.close_until(max(left for left, _key in account.exits))

    return Replay(
        initial_capital=initial_capital,
        final_balance=account.balance,
        max_drawdown_pct=account.deepest_pct,
        max_drawdown_abs=account.deepest_abs,
        most_open=account.most,
        members=account.outcomes,
        curve=account.curve,
    )


def daily(curve: Sequence[Point]) -> list[Point]:
    """The curve thinned to the last point of each UTC day — what is stored and drawn. The
    drawdown is measured on the whole curve before this, never on the thinned one."""
    last: dict[dt.date, Point] = {}
    for point in curve:
        last[point.time.date()] = point
    return [last[day] for day in sorted(last)]


def yearly_returns(curve: Sequence[Point], initial_capital: Decimal) -> dict[int, Decimal]:
    """Each calendar year's return on the marked equity it started with."""
    out: dict[int, Decimal] = {}
    start = initial_capital
    end_of: dict[int, Decimal] = {}
    for point in curve:
        end_of[point.time.year] = point.equity
    for year in sorted(end_of):
        out[year] = (end_of[year] - start) / start if start > 0 else _ZERO
        start = end_of[year]
    return out


__all__ = [
    "Limits",
    "Mark",
    "MemberOutcome",
    "MemberTrade",
    "Point",
    "Replay",
    "Skip",
    "daily",
    "replay",
    "yearly_returns",
]
