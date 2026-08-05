"""`StructuralTrail` on its own: the level market structure names for an open trade.

The rule is exercised end to end by the two setups that use it — `test_setups.py` for the SMC
family and `test_ponto_continuo.py` for the swing one — where the breaks come from a real
`MarketStructure` fed real candles. These tests do the opposite: they hand the trail breaks built by
hand, which is the only way to reach cases a candle stream cannot conveniently produce (a *bullish*
change of character while a long is open) and to state the trade-keyed counter as a contract rather
than as a consequence of some scenario.

`tighten` and `breakeven_candidate` are not tested here. They hold no state and are covered by the
suites of both consumers, where a level's fate — sent, or refused for not improving — is the thing
those tests are about.
"""

import datetime as dt
from decimal import Decimal

from tradeforge_engine.conduction import StructuralTrail
from tradeforge_engine.domain import Position, Side
from tradeforge_engine.structure import StructureBreak, StructureKind, Trend
from tradeforge_engine.testing import AAPL, HOUR, START


def _position(*, side: Side = Side.LONG, entry: str = "100", at: dt.datetime = START) -> Position:
    return Position(
        symbol=AAPL.symbol,
        side=side,
        volume=Decimal(1),
        entry_price=Decimal(entry),
        entry_time=at,
        stop_loss=Decimal("95"),
        initial_stop_loss=Decimal("95"),
    )


def _break(
    *,
    kind: StructureKind = StructureKind.BOS,
    trend: Trend = Trend.BULLISH,
    level: str = "110",
    origin: str = "97",
) -> StructureBreak:
    return StructureBreak(
        kind=kind,
        trend=trend,
        level=Decimal(level),
        time=START,
        # Unread by the conduction, which cares only about the level and the origin. Set to
        # the same instant rather than an invented earlier one: a number nothing reads that
        # looks measured is worse than one that plainly is not.
        level_time=START,
        origin=Decimal(origin),
        origin_time=START,
    )


def test_the_first_break_in_favour_names_the_entry_price() -> None:
    trail = StructuralTrail()
    assert trail.candidate(position=_position(), break_=_break()) == Decimal(100)


def test_every_break_after_the_first_names_its_own_leg_origin() -> None:
    """Not the level that was broken: `level` is where the move went *through*, `origin` is where it
    came *from* — the price at which the trend would be disproven."""
    trail, position = StructuralTrail(), _position()
    trail.candidate(position=position, break_=_break())
    assert trail.candidate(position=position, break_=_break(origin="103")) == Decimal(103)
    assert trail.candidate(position=position, break_=_break(origin="107")) == Decimal(107)


def test_a_bar_with_no_break_names_nothing() -> None:
    assert StructuralTrail().candidate(position=_position(), break_=None) is None


def test_a_break_against_the_trade_names_nothing() -> None:
    trail = StructuralTrail()
    assert trail.candidate(position=_position(), break_=_break(trend=Trend.BEARISH)) is None


def test_a_change_of_character_names_nothing_even_in_our_favour() -> None:
    """Only a BOS counts, and a favourable CHoCH is not a case that was excluded — it is a case that
    does not occur: inside a conducted trade the structure already points our way, so the change of
    character that turns up is the opposite one, and it ends the trade rather than conducting it.
    This test guards the code path, not the method.
    """
    trail = StructuralTrail()
    favourable = _break(kind=StructureKind.CHOCH, trend=Trend.BULLISH)
    assert trail.candidate(position=_position(), break_=favourable) is None


def test_a_break_the_trail_refused_does_not_count_toward_the_first() -> None:
    """A CHoCH and a break against the trade both name nothing *and* leave the count alone, so the
    next real BOS is still this trade's first and still means breakeven. Counting them would put the
    stop at a leg origin on the first break that mattered — below the entry on a long, so the trade
    would go on carrying risk the method says it already gave up.
    """
    trail, position = StructuralTrail(), _position()
    trail.candidate(position=position, break_=_break(kind=StructureKind.CHOCH))
    trail.candidate(position=position, break_=_break(trend=Trend.BEARISH))
    trail.candidate(position=position, break_=None)
    assert trail.candidate(position=position, break_=_break()) == Decimal(100)


def test_a_new_trade_counts_from_zero() -> None:
    """The counter is keyed to `entry_time`, not reset by an event, and this is the contract that
    makes the difference visible: the second trade's first break must mean breakeven again.

    Keyed the other way — zeroed when a fill is *observed* — this holds only for as long as every
    path that opens a trade runs through the observer. The day one does not, the new trade's first
    break silently takes the second break's branch and the stop lands at a leg origin below the
    entry. Keying it means a trade that never announced itself still counts from zero, because it is
    a different trade.
    """
    trail = StructuralTrail()
    first = _position(at=START)
    trail.candidate(position=first, break_=_break())
    assert trail.candidate(position=first, break_=_break(origin="103")) == Decimal(103)

    second = _position(entry="120", at=START + 10 * HOUR)
    assert trail.candidate(position=second, break_=_break(origin="115")) == Decimal(120)
    assert trail.candidate(position=second, break_=_break(origin="118")) == Decimal(118)


def test_the_short_mirror_names_the_entry_then_the_origins_of_bearish_breaks() -> None:
    trail = StructuralTrail()
    position = _position(side=Side.SHORT, entry="100")
    assert trail.candidate(position=position, break_=_break(trend=Trend.BEARISH)) == Decimal(100)
    assert trail.candidate(
        position=position, break_=_break(trend=Trend.BEARISH, origin="104")
    ) == Decimal(104)
    # A bullish break is now the one against the trade.
    assert trail.candidate(position=position, break_=_break(trend=Trend.BULLISH)) is None
