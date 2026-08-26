"""Accepted is not filled, and the terminal says both in the same breath.

⚠️ **Every answer below is labelled either RECORDED or CONSTRUCTED, and the labels are the
point of the file.** `MT5Gateway`'s docstring argues, correctly, that mocking `order_send`
proves only that the gateway calls a function the gateway also describes — the `fake que diverge`
failure this project has already paid for. A *recording* escapes that: it is evidence about the
venue, and reading it is how a Linux CI box asks a question that otherwise needs Windows, a
terminal and an open market. A *construction* does not escape it, and gets to stand only where it
states a rule this code must hold to whatever a venue answers. Filing the second with the first
is how a suite starts believing it measured something it invented.

The one recording, 26/08/2026, Tradeview-Demo, EURUSD, a buy limit resting 200 points below
the market: `retcode=10009`, `deal=0`, `volume='0.01'`, `price='0.0'`. The account then held one
pending order, **zero positions and zero deals**. That is `RESTING`, and it is the answer the fix
in `_placement` exists for.

`FILLED` was constructed at first — no market order had been sent — and was replaced the same day
by the real answer, from a 0.01 EURUSD position opened and closed on the demo. Its `bid` and `ask`
came back **`'0.0'` on an executed deal**, which is the measurement that justifies reading the
quote *before* sending: after the fact it does not exist anywhere. Only
`RESTING_ECHOING_ITS_PRICE` is still a construction, and it says so.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from tradeforge_engine.domain import Side
from tradeforge_executor.gateway import MAGIC, MT5Gateway, Placement
from tradeforge_executor.wire import MAX_CLIENT_ID


class _Retcodes:
    """The two constants `_placement` reads off the module. Not a mock of any behaviour."""

    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008


@dataclass
class _Answer:
    """An `OrderSendResult`, field for field."""

    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    comment: str

    def _asdict(self) -> dict[str, Any]:
        return dict(vars(self))


# --- RECORDED -------------------------------------------------------------------------
RESTING = _Answer(
    retcode=10009,
    deal=0,
    order=47_084_649,
    volume=0.01,
    price=0.0,
    comment="Request executed",
)
"""**RECORDED.** A limit order the venue accepted and parked. `volume` is the request, echoed
back, and it is the field that used to be read as a fill."""

FILLED = _Answer(
    retcode=10009,
    deal=35_176_079,
    order=47_096_513,
    volume=0.01,
    price=1.16524,
    comment="Request executed",
)
"""**RECORDED.** A market buy that actually traded: 0.01 EURUSD on the demo, 26/08.

Same retcode as `RESTING`, same echoed volume — `deal` is the only field that differs, which is
exactly why it is the one both readings are derived from.

⚠️ The answer also carried `bid='0.0'` and `ask='0.0'` on a deal that plainly had both. That is
why `send` reads the quote before the order goes out: afterwards there is nowhere to get it."""


# --- CONSTRUCTED ----------------------------------------------------------------------

RESTING_ECHOING_ITS_PRICE = _Answer(
    retcode=10009,
    deal=0,
    order=47_084_651,
    volume=0.01,
    price=1.16460,
    comment="Request executed",
)
"""⚠️ **Constructed, not recorded.** Tradeview answers a placement with `price=0.0`, so on that
venue "no deal" and "no price" happen to coincide and nothing can tell the two readings apart —
a mutant that gates the volume on the deal and leaves the price ungated survives every test that
can be written against the recordings above.

The rule does not depend on the coincidence: **an order that has not executed has no execution
price**, and `OrderSendResult.price` is documented as the *deal* price, which a placement does
not have. A venue echoing the requested price back is the shape that would turn that into a
`Placement` claiming 1.16460 was paid for a position nobody holds — read later by the panel, by
reconciliation, by anything that trusts the field's name."""


QUOTED_SPREAD = Decimal("1.16521") - Decimal("1.16514")
"""**RECORDED.** The quote standing when the market order went out: bid 1.16514, ask 1.16521.

`MT5Gateway.send` reads this *before* `order_send` and hands it down, because `order_send`
answers with the price that traded and the quote either side of it is gone a second later."""


def placement_of(answer: _Answer, *, spread: Decimal | None = QUOTED_SPREAD) -> Placement:
    return MT5Gateway()._placement(_Retcodes(), answer, spread=spread)


def test_a_resting_limit_order_did_not_fill_anything() -> None:
    """⚠️ The measurement this whole file exists for.

    `retcode=10009` is *DONE* — not even *PLACED* — and `volume` comes back as the volume that
    was **asked for**. A reading that trusts either reports a fill for an order still sitting in
    the book: "decide on the breakout, fill on the breakout", arriving through the venue instead
    of through the broker, where no engine guard is watching for it.
    """
    placement = placement_of(RESTING)

    assert placement.accepted, "the venue did take the order; it just has not executed it"
    assert placement.resting
    assert placement.filled_volume == Decimal(0), "a resting order was read as a fill"
    assert placement.price is None, "the echoed 0.0 was read as a price"
    assert placement.deal is None
    assert not placement.partial
    assert not placement.is_short_of(Decimal("0.10"))


def test_a_resting_order_reports_no_price_even_when_the_venue_echoes_one() -> None:
    """The case the recordings cannot reach — see `RESTING_ECHOING_ITS_PRICE`.

    Without this, the deal gate around the price is dead logic: on a venue that answers `0.0`,
    deleting it changes no observable behaviour, and a guard nothing can observe is a guard that
    is not there.
    """
    placement = placement_of(RESTING_ECHOING_ITS_PRICE)

    assert placement.resting
    assert placement.filled_volume == Decimal(0)
    assert placement.price is None, "the requested price was reported as a price that was paid"


def test_a_real_deal_is_still_read_as_a_fill() -> None:
    """The separating half. Same retcode, same echoed volume, different `deal` — and the two
    must not come out alike, or the fix above would just be "never fill anything".

    Both halves are recordings, from the same terminal on the same day.
    """
    placement = placement_of(FILLED)

    assert placement.accepted
    assert not placement.resting
    assert placement.filled_volume == Decimal("0.01")
    assert placement.price == Decimal("1.16524")
    assert placement.deal == 35_176_079
    assert placement.partial


def test_the_price_goes_home_as_decimal_text_not_a_float() -> None:
    """`Decimal(1.16524)` is not 1.16524, and a tick that survives the venue must survive us."""
    assert str(placement_of(FILLED).price) == "1.16524"


def test_a_fill_carries_the_quote_it_crossed() -> None:
    """The session cannot recover this later, and it cannot price the fill without it: MT5's
    bars are bid-based, so a buy's fill price sits above the bar it happened in."""
    assert placement_of(FILLED).spread == Decimal("0.00007")


def test_a_resting_order_crossed_nothing_so_it_carries_no_spread() -> None:
    """Same gate as the volume and the price: nothing executed, so there is nothing to price.

    ⚠️ `None`, not zero. Zero is a real quote at a quiet hour on this venue — measured — so it
    cannot double as "there was no fill", or the two become the same word for "charge nothing"
    and only one of them means it.
    """
    assert placement_of(RESTING).spread is None


def test_a_fill_that_cannot_say_what_it_crossed_cannot_be_constructed() -> None:
    """The mirror of the deal guard: an execution nobody can price would be recorded at a
    made-up cost, and zero is the made-up cost that looks most like a measurement."""
    with pytest.raises(ValueError, match="no quote"):
        placement_of(FILLED, spread=None)


def test_a_negative_spread_is_refused() -> None:
    """A spread is a magnitude. A negative one is the ask below the bid, which is either a
    crossed book or a sign error — and as a cost it would be money appearing from nowhere."""
    with pytest.raises(ValueError, match="magnitude"):
        placement_of(FILLED, spread=Decimal("-0.00001"))


def test_a_refusal_carries_no_deal_and_no_volume() -> None:
    refused = _Answer(
        retcode=10027, deal=0, order=0, volume=0.0, price=0.0, comment="AutoTrading disabled"
    )
    placement = placement_of(refused)

    assert not placement.accepted
    assert not placement.resting, "a refusal is not an order waiting in the book"
    assert placement.filled_volume == Decimal(0)
    assert placement.deal is None


def test_the_verbatim_answer_survives_into_the_audit_trail() -> None:
    """Nothing is lost by not trusting `volume`: `raw` still carries what the terminal said,
    including the echo, which is what an incident asks about."""
    raw = placement_of(RESTING).raw

    assert raw["volume"] == "0.01", "the echoed volume vanished from the evidence"
    assert raw["deal"] == 0, "`_jsonable` stringifies the floats and leaves integers alone"


def test_a_filled_volume_with_no_deal_cannot_be_constructed() -> None:
    """⚠️ The guard that keeps a **fake** from telling the lie the terminal tells.

    `_placement` derives both fields from one answer and cannot build an inconsistent pair, so
    this can only ever fire on a hand-built `Placement` — which is precisely where a divergence
    from the real venue goes unnoticed. It fired on three of this suite's own fakes the day it
    was added.
    """
    with pytest.raises(ValueError, match="no deal ticket"):
        Placement(
            accepted=True,
            ticket=99,
            filled_volume=Decimal("0.10"),
            price=Decimal("1.10000"),
            retcode=10009,
            comment="done",
            raw={},
        )


# --- the name, on its way to the venue -------------------------------------------------


def test_a_name_longer_than_the_venue_keeps_is_cut_to_what_it_keeps() -> None:
    """⚠️ **Inside `MT5Gateway`, and tested anyway** — which is not a breach of that class's
    doctrine. The rule there is that mocking `order_send` proves only that the gateway calls a
    function the gateway also describes. `_comment` never reaches the terminal: it is arithmetic
    on a string, and arithmetic on a string is exactly the kind of thing a Linux CI box can ask
    about with no MetaTrader in sight.

    The cut is real: MT5 keeps 31 characters of an order comment and drops the rest without a
    word. Every name in this system ends in the part that distinguishes it, so the drop does not
    shorten a name — it **merges** names.
    """
    name = "z" * 40
    assert MT5Gateway()._comment(name) == "z" * MAX_CLIENT_ID


def test_a_name_that_fits_is_handed_over_whole() -> None:
    """The separating half, and a realistic name rather than `"z" * 40` on purpose.

    What it catches that the length assertions cannot is a comment that comes out the **same
    length and a different name** — a normalisation, a case fold, a character the venue dislikes
    being swapped. Verified by mutation: replacing `-` with `_` inside the cut fails this test
    and only this test.

    ⚠️ It does *not* catch a cut that is merely too short by a little (`[:30]`), which a 22
    character name passes through unchanged. That mutant dies on the tests above instead.
    """
    assert MT5Gateway()._comment("demand-20260826T1215-7") == "demand-20260826T1215-7"


def test_the_venue_s_limit_is_read_from_the_wire_not_from_a_number_here() -> None:
    """Both ends need it — the venue's limit is what makes a name valid, and the side that
    *chooses* names is three processes away and must not learn MetaTrader to find out."""
    assert len(MT5Gateway()._comment("z" * 40)) == MAX_CLIENT_ID


# --- withdrawing, and the rule that one answer is not enough ---------------------------


class _Removal:
    """An `OrderSendResult` for a `TRADE_ACTION_REMOVE`. Shape recorded on 26/08: the terminal
    answered `retcode=10009` with the comment "Request executed"."""

    def __init__(self, retcode: int, comment: str = "Request executed") -> None:
        self.retcode = retcode
        self.comment = comment


class _Resting:
    def __init__(self, ticket: int) -> None:
        self.ticket = ticket


def withdrawal_of(*codes: int) -> Placement:
    orders = [_Resting(47_084_649 + index) for index in range(len(codes))]
    answers = [_Removal(code) for code in codes]
    return MT5Gateway()._withdrawal(_Retcodes(), "zone-42", orders, answers)


def test_one_order_removed_cleanly_is_an_accepted_withdrawal() -> None:
    placement = withdrawal_of(10009)

    assert placement.accepted
    assert placement.raw["withdrew"] == 1
    assert placement.filled_volume == Decimal(0), "a withdrawal filled something"
    assert placement.deal is None


def test_a_withdrawal_is_not_a_success_unless_every_answer_is() -> None:
    """⚠️ One name should match one order — but "should" is a fact about the strategy, not about
    the account. A partial withdrawal reported as a success leaves an order live at the venue
    that the session has already written off, and re-arms nothing to notice it.
    """
    placement = withdrawal_of(10009, 10013)

    assert not placement.accepted, "a failed removal was reported as a clean withdrawal"
    assert placement.raw["withdrew"] == 0, "the count claimed a withdrawal that did not happen"
    assert placement.raw["retcodes"] == [10009, 10013], "the evidence lost the failing answer"


def test_every_ticket_touched_is_in_the_evidence() -> None:
    """`order_audit.response` is what an incident reads. A projection that recorded only the
    first ticket would leave the second one unaccounted for on the exact row explaining why."""
    placement = withdrawal_of(10009, 10009)

    assert placement.raw["tickets"] == [47_084_649, 47_084_650]
    assert placement.raw["withdrew"] == 2


# --- whose position is it, anyway ------------------------------------------------------


class _Position:
    """A position as `positions_get` reports one. Fields recorded 26/08 from the real thing:
    `ticket=47096513 type=0 volume=0.01 price_open=1.16524 sl=1.16014 comment='probe-a3b-sltp'`.
    """

    def __init__(  # noqa: PLR0913 — the recorded fields, each with its recorded value
        self,
        ticket: int,
        *,
        magic: int,
        kind: int = 0,
        sl: float = 1.16014,
        symbol: str = "EURUSD",
        volume: float = 0.01,
        price_open: float = 1.16524,
    ) -> None:
        self.ticket = ticket
        self.magic = magic
        self.type = kind
        self.sl = sl
        self.symbol = symbol
        self.volume = volume
        self.price_open = price_open


class _Terminal:
    """A terminal that holds a fixed list of positions and answers nothing else.

    ⚠️ **Admissible where a mock of `order_send` would not be.** `MT5Gateway`'s doctrine is that
    mocking the venue proves only that this file calls a function it also describes. What is
    exercised here is not a call — it is a **decision**, the filter that establishes a position
    belongs to this executor, and the decision is the entire safety story of `tighten`: the
    `TRADE_ACTION_SLTP` request carries no magic, so a ticket that escaped this filter is acted
    on with no further question asked. The position shape is a recording; the filtering is ours.
    """

    def __init__(self, *positions: _Position) -> None:
        self._positions = positions
        self.asked: list[str] = []

    def positions_get(self, *, symbol: str) -> tuple[_Position, ...]:
        self.asked.append(symbol)
        return self._positions


def test_only_positions_carrying_our_magic_are_ours() -> None:
    """⚠️ The account holds a manual trade and another advisor's. Neither is this executor's to
    move, and `TRADE_ACTION_SLTP` will not ask: it carries no magic and no symbol, so whatever
    ticket comes out of here is what gets modified."""
    terminal = _Terminal(
        _Position(11, magic=0),  # opened by hand
        _Position(22, magic=999_999),  # another expert advisor
        _Position(33, magic=MAGIC),  # ours
    )

    held = MT5Gateway(terminal=terminal).connect().held("EURUSD")

    assert held is not None
    assert held.ticket == 33, "a position belonging to somebody else was reported as ours"


def test_an_account_holding_only_other_peoples_positions_reports_nothing() -> None:
    """The separating half: without it, a filter that simply took the first position would pass
    the test above whenever ours happens to be listed first."""
    terminal = _Terminal(_Position(11, magic=0), _Position(22, magic=999_999))

    assert MT5Gateway(terminal=terminal).connect().held("EURUSD") is None


def test_a_position_with_no_stop_reports_none_not_zero() -> None:
    """MT5 reports an absent stop as `0.0`, and a stop *at* zero is not a level anybody set.
    Collapsed, "unprotected" and "protected at nothing" become the same answer — and only one of
    them makes arming a stop a tightening."""
    terminal = _Terminal(_Position(33, magic=MAGIC, sl=0.0))

    held = MT5Gateway(terminal=terminal).connect().held("EURUSD")

    assert held is not None
    assert held.stop_loss is None


def test_a_short_is_read_as_a_short() -> None:
    """`type=1` is a sell. Read as a long, every direction check downstream inverts — and the
    inversion is silent, because a loosening looks like a tightening from the wrong side."""
    terminal = _Terminal(_Position(33, magic=MAGIC, kind=1))

    held = MT5Gateway(terminal=terminal).connect().held("EURUSD")

    assert held is not None
    assert held.side is Side.SHORT


def test_two_of_our_positions_is_an_error_rather_than_a_guess() -> None:
    """Phase 1 holds one at a time by construction, so two under this magic means something went
    wrong upstream. Picking the first would move the stop of whichever the terminal happened to
    list first, silently, on the one instruction whose whole job is to reduce risk."""
    terminal = _Terminal(_Position(33, magic=MAGIC), _Position(34, magic=MAGIC))

    with pytest.raises(ConnectionError, match="will not guess"):
        MT5Gateway(terminal=terminal).connect().held("EURUSD")
