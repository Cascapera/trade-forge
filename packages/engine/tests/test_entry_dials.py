"""`DIALS_READ`: which entry point reads which dial — what a sweep shares equivalent runs on.

A sweep that runs `stop_buffer=0.2` under `martelo` once for all four buffers (24/09) is only
right if the martelo really never reads the buffer. A table that said so wrongly would merge runs
that differ, silently, in every sweep after. So the table is pinned against `activation_for`
itself, both ways — an unread dial builds the same activation, a read one a different one — and
then on the whole compiled setup: every unread dial changed leaves its entire state as it was.
"""

import pickle
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.average_setups import (
    AVERAGE_DIALS,
    AVERAGE_DIALS_READ,
    AverageEntryPoint,
    PatternWatch,
)
from tradeforge_engine.bar_setups import GiftStop
from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.setup_factory import unread_params
from tradeforge_engine.setups import DIALS_READ, ENTRY_DIALS, ZoneEntryPoint, activation_for
from tradeforge_engine.strategy import compile_strategy
from tradeforge_engine.testing import bar

# The dials at one value, and each at another. `activation_for` takes the buffer as a Decimal and
# the document as a float; both are given so the table is tested at the call it describes.
_BASE: dict[str, object] = {
    "stop_buffer": Decimal("0.1"),
    "gift_stop": GiftStop.GIFT,
    "volume_filter": False,
}
_OTHER: dict[str, object] = {
    "stop_buffer": Decimal("0.2"),
    "gift_stop": GiftStop.FORCA,
    "volume_filter": True,
}
_DOCUMENT_OTHER: dict[str, object] = {
    "stop_buffer": 0.2,
    "gift_stop": "forca",
    "volume_filter": True,
}


def test_every_entry_point_is_in_the_table_and_names_only_dials() -> None:
    assert set(DIALS_READ) == set(ZoneEntryPoint)
    assert all(read <= ENTRY_DIALS for read in DIALS_READ.values())
    assert set(_BASE) == ENTRY_DIALS


@pytest.mark.parametrize("entry", list(ZoneEntryPoint))
@pytest.mark.parametrize("dial", sorted(ENTRY_DIALS))
def test_the_table_is_what_activation_for_reads(entry: ZoneEntryPoint, dial: str) -> None:
    """Exactly, not approximately: a dial the table calls unread must not reach the activation,
    and a dial it calls read must — so the table can neither merge runs nor leave speed behind."""
    base = activation_for(entry, **_BASE)  # type: ignore[arg-type]  # the dials, by name
    moved = activation_for(entry, **{**_BASE, dial: _OTHER[dial]})  # type: ignore[arg-type]

    if dial in DIALS_READ[entry]:
        assert moved != base
    else:
        assert moved == base


class TestUnreadParams:
    def test_names_the_dials_the_entry_point_leaves_alone(self) -> None:
        def unread(entry: str) -> frozenset[str]:
            return unread_params({"type": "structure_choch", "params": {"entry_point": entry}})

        assert unread("martelo") == ENTRY_DIALS
        assert unread("gift") == {"stop_buffer"}
        assert unread("barra_ignorada") == {"stop_buffer", "gift_stop"}
        assert unread("edge") == {"gift_stop", "volume_filter"}

    def test_reads_the_edge_when_no_entry_point_is_named(self) -> None:
        node = {"type": "structure_continuation", "params": {}}
        assert unread_params(node) == {"gift_stop", "volume_filter"}

    @pytest.mark.parametrize(
        "node",
        [
            {"type": "mme9_turn", "params": {"entry_point": "martelo"}},
            {"type": "structure_choch", "params": {"entry_point": "nowhere"}},
            {"type": "structure_choch", "params": {"entry_point": 3}},
            {"type": "structure_choch", "params": "not a mapping"},
            {"params": {"entry_point": "martelo"}},
        ],
    )
    def test_claims_nothing_it_cannot_vouch_for(self, node: dict[str, object]) -> None:
        """Nothing unread is never a wrong answer — it only runs a point that could have been
        shared. The other mistake is the one that must be impossible."""
        assert unread_params(node) == frozenset()


def _document(kind: str, params: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "dials",
        "timeframe": "M15",
        "setup": {"type": kind, "params": params},
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 2}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def _state(kind: str, params: dict[str, object]) -> bytes:
    """Everything a freshly compiled setup holds, as bytes two equal states spell the same way."""
    return pickle.dumps(compile_strategy(_document(kind, params)))


@pytest.mark.parametrize("kind", ["structure_choch", "structure_continuation"])
@pytest.mark.parametrize("entry", list(ZoneEntryPoint))
@pytest.mark.parametrize("side", ["long", "short", "both"])
@pytest.mark.parametrize("htf", [None, "H4"])
def test_unread_dials_leave_the_whole_setup_as_it_was(
    kind: str, entry: ZoneEntryPoint, side: str, htf: str | None
) -> None:
    """⚠️ **The proof the sweep's sharing rests on, and why it is about state, not trades.**

    The engine keeps no reference to the document it was compiled from and is deterministic, so
    two setups whose whole state is equal when built run the same over any candles. Comparing
    that state reaches every place a dial could land — the conduction, the gate, the qualifier —
    where a run over random candles reaches only the few that trade: measured by the guardian,
    15 of these 18 setup-by-entry pairs closed no trade in any example, and a strategy that read
    the buffer for its breakeven passed the whole suite.
    """
    base: dict[str, object] = {
        "entry_point": entry.value,
        "side": side,
        "stop_buffer": 0.1,
        "gift_stop": "gift",
        "volume_filter": False,
        **({"htf": htf, "htf_offset": 3} if htf is not None else {}),
    }
    unread = unread_params({"type": kind, "params": base})

    for dial in sorted(ENTRY_DIALS):
        moved = _state(kind, {**base, dial: _DOCUMENT_OTHER[dial]})
        if dial in unread:
            assert moved == _state(kind, base), f"{dial} reaches the {entry.value} setup"
        else:
            assert moved != _state(kind, base), f"{dial} never reaches the {entry.value} setup"


# --------------------------------------------------------------------------- #
# The average setups: the same claim, on `AVERAGE_DIALS_READ` (24/09)          #
# --------------------------------------------------------------------------- #


@st.composite
def _touching_walk(draw: st.DrawFn) -> list[Candle]:
    """Bars that wander around a slow line, so they touch it and form the patterns often."""
    count = draw(st.integers(min_value=60, max_value=200))
    step = st.decimals(min_value="-2", max_value="2", places=1)
    wick = st.decimals(min_value="0", max_value="2", places=1)
    candles: list[Candle] = []
    price = Decimal(100)
    for index in range(count):
        open_ = price
        close = max(Decimal(20), open_ + draw(step))
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        volume = draw(st.integers(min_value=1, max_value=500))
        candles.append(
            bar(
                index,
                open_=str(open_),
                close=str(close),
                high=str(high),
                low=str(low),
                tick_volume=volume,
            )
        )
        price = close
    return candles


def _orders(watch: PatternWatch, candles: list[Candle]) -> list[object]:
    """What the watch wants resting after each bar, against a three-bar mean of the closes."""
    out: list[object] = []
    for index, candle in enumerate(candles):
        window = candles[max(0, index - 2) : index + 1]
        average = sum((one.close for one in window), Decimal(0)) / len(window)
        out.append(watch.observe(candle, average, tick=Decimal("0.01")))
    return out


def test_every_average_entry_point_is_in_the_table_and_names_only_dials() -> None:
    assert set(AVERAGE_DIALS_READ) == set(AverageEntryPoint)
    assert all(read <= AVERAGE_DIALS for read in AVERAGE_DIALS_READ.values())


@pytest.mark.parametrize(
    "entry", [one for one in AverageEntryPoint if one is not AverageEntryPoint.CLASSIC]
)
@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@settings(max_examples=40, deadline=None)
@given(candles=_touching_walk())
def test_a_watch_answers_the_same_whatever_its_unread_dials_say(
    entry: AverageEntryPoint, side: Side, candles: list[Candle]
) -> None:
    """Built by hand — around `watch_for`, which would hide the question — with every dial the
    table calls unread moved: bar for bar, the same order resting. A dial `_second_bar` reads and
    the table left out would part the two the first time the pattern forms."""
    base = PatternWatch(entry_point=entry, side=side)
    unread = AVERAGE_DIALS - AVERAGE_DIALS_READ[entry]
    moved = PatternWatch(
        entry_point=entry,
        side=side,
        gift_stop=GiftStop.FORCA if "gift_stop" in unread else GiftStop.GIFT,
        volume_filter="volume_filter" in unread,
    )

    assert _orders(moved, candles) == _orders(base, candles)


@pytest.mark.parametrize("kind", ["mme9_breakout", "ponto_continuo"])
@pytest.mark.parametrize("entry", list(AverageEntryPoint))
@pytest.mark.parametrize("side", ["long", "short", "both"])
@pytest.mark.parametrize("long_average", [None, 50])
def test_unread_average_dials_leave_the_whole_setup_as_it_was(
    kind: str, entry: AverageEntryPoint, side: str, long_average: int | None
) -> None:
    """The state proof `test_unread_dials_leave_the_whole_setup_as_it_was` makes for the
    structure setups, for the two average setups with entry points."""
    base: dict[str, object] = {
        "period": 9,
        "entry_point": entry.value,
        "side": side,
        "gift_stop": "gift",
        "volume_filter": False,
        **({"long_average_period": long_average} if long_average is not None else {}),
    }
    unread = unread_params({"type": kind, "params": base})
    other = {"gift_stop": "forca", "volume_filter": True}

    for dial in sorted(AVERAGE_DIALS):
        moved = _state(kind, {**base, dial: other[dial]})
        if dial in unread:
            assert moved == _state(kind, base), f"{dial} reaches the {entry.value} {kind}"
        else:
            assert moved != _state(kind, base), f"{dial} never reaches the {entry.value} {kind}"


class TestUnreadAverageParams:
    """The table pinned by value, as `TestUnreadParams` pins the structure one: the state proof
    alone cannot tell a dial the watch reads from one it is merely handed (guardian, 24/09)."""

    @pytest.mark.parametrize("kind", ["mme9_breakout", "ponto_continuo"])
    def test_names_the_dials_each_entry_leaves_alone(self, kind: str) -> None:
        def unread(entry: str) -> frozenset[str]:
            return unread_params({"type": kind, "params": {"entry_point": entry}})

        assert unread("classic") == {"gift_stop", "volume_filter"}
        assert unread("martelo") == {"gift_stop", "volume_filter"}
        assert unread("martelo_forca") == {"gift_stop", "volume_filter"}
        assert unread("gift") == frozenset()
        assert unread("barra_ignorada") == {"gift_stop"}

    def test_reads_the_classic_when_no_entry_point_is_named(self) -> None:
        assert unread_params({"type": "mme9_breakout", "params": {}}) == {
            "gift_stop",
            "volume_filter",
        }

    def test_claims_nothing_for_an_entry_it_does_not_know(self) -> None:
        node = {"type": "mme9_breakout", "params": {"entry_point": "nowhere"}}
        assert unread_params(node) == frozenset()
