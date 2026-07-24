"""The condition tree, evaluated against hand-built contexts.

Two things carry the weight here. First, the difference between a *level* (`gt`, true for as
long as the relation holds) and an *edge* (`crosses_above`, true only on the bar it flips) —
the distinction that stops a strategy re-entering a trend it already owns. Second, that a
value which does not exist yet — an indicator still warming up — makes a comparison false
rather than raising, so warm-up is silent and safe.
"""

from decimal import Decimal

import pytest

from tradeforge_engine.domain import Candle, EvalContext, Money
from tradeforge_engine.errors import EngineError
from tradeforge_engine.expressions import compile_condition, compile_constant, compile_operand
from tradeforge_engine.testing import bar


def _ctx(
    candles: tuple[Candle, ...] = (),
    indicators: dict[str, tuple[Money | None, ...]] | None = None,
) -> EvalContext:
    return EvalContext(candles=candles, indicator_values=indicators or {})


# --------------------------------------------------------------------------- #
# Operand resolution                                                            #
# --------------------------------------------------------------------------- #


def test_price_ref_reads_the_current_candle() -> None:
    candle = bar(0, open_="1.0", close="1.5", high="1.6", low="0.9")
    assert compile_operand("price.close").resolve(_ctx((candle,)), 0) == Decimal("1.5")
    assert compile_operand("price.high").resolve(_ctx((candle,)), 0) == Decimal("1.6")


def test_candle_ref_reaches_a_closed_bar_back() -> None:
    now = bar(1, open_="2.0", close="2.0")
    prev = bar(0, open_="1.0", close="1.0")
    # newest-first: candles[0] is now, candles[1] is one bar back
    assert compile_operand("candle[-1].close").resolve(_ctx((now, prev)), 0) == Decimal("1.0")


def test_a_ref_past_the_start_of_history_is_none() -> None:
    """Early in a run there simply is no candle there — not an error, just nothing to compare."""
    only = bar(0, open_="1.0", close="1.0")
    assert compile_operand("candle[-1].close").resolve(_ctx((only,)), 0) is None


def test_indicator_ref_reads_the_named_value() -> None:
    ctx = _ctx(indicators={"sma_fast": (Decimal("1.2345"), None)})
    assert compile_operand("sma_fast").resolve(ctx, 0) == Decimal("1.2345")


def test_operand_lookback_is_the_candle_depth_it_needs() -> None:
    assert compile_operand("price.close").lookback == 0
    assert compile_operand("candle[-3].high").lookback == 3
    assert compile_operand("sma_fast").lookback == 0


def test_a_constant_resolves_to_the_same_value_at_every_shift() -> None:
    """A literal never warms up and never runs out of history: `RSI < 30` needs the 30 to exist
    on this bar and the previous one, so an edge like `crosses_below 30` has a steady level."""
    constant = compile_constant(30)
    assert constant.resolve(_ctx(), 0) == Decimal("30")
    assert constant.resolve(_ctx(), 1) == Decimal("30")
    assert constant.lookback == 0


def test_a_constant_is_parsed_through_decimal_not_binary_float() -> None:
    """The threshold typed as 0.1 must be exactly 0.1 in the comparison — `float("0.1")` is
    0.1000000000000000055..., and a strategy that trips at a boundary would trip wrong."""
    assert compile_constant(0.1).resolve(_ctx(), 0) == Decimal("0.1")


def test_a_comparison_against_a_constant_threshold() -> None:
    """`rsi < 30`: true when the indicator is below the level, false at or above it."""
    node = {"op": "lt", "left": {"ref": "rsi"}, "right": {"value": 30}}
    assert compile_condition(node).evaluate(_ctx(indicators={"rsi": (Decimal("25"),)})) is True
    assert compile_condition(node).evaluate(_ctx(indicators={"rsi": (Decimal("40"),)})) is False


def test_an_edge_operator_crosses_a_constant_threshold() -> None:
    """`rsi crosses_below 30` — the headline of the literal-operand feature, end to end. True only
    on the bar the indicator moves from at-or-above 30 to below it, not while it sits below. The
    constant is steady at both shifts, so it is the indicator's motion the edge measures.

    Indicator history is newest-first: index 0 is this bar, index 1 the previous one.
    """
    node = {"op": "crosses_below", "left": {"ref": "rsi"}, "right": {"value": 30}}
    crossing = _ctx(indicators={"rsi": (Decimal("25"), Decimal("35"))})  # 35 -> 25: pierces 30
    already_below = _ctx(indicators={"rsi": (Decimal("20"), Decimal("25"))})  # 25 -> 20: both below
    assert compile_condition(node).evaluate(crossing) is True
    assert compile_condition(node).evaluate(already_below) is False


def test_a_constant_operand_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(EngineError, match="value must be a number"):
        compile_constant("30")


def test_a_boolean_is_not_a_valid_constant() -> None:
    """bool is an int subclass, so `True` would otherwise sneak through as 1 — refused."""
    with pytest.raises(EngineError, match="value must be a number"):
        compile_constant(True)


# --------------------------------------------------------------------------- #
# Level operators                                                               #
# --------------------------------------------------------------------------- #


def test_level_operators_ask_only_about_this_bar() -> None:
    ctx = _ctx(indicators={"a": (Decimal("2"),), "b": (Decimal("1"),)})
    assert compile_condition({"op": "gt", "left": {"ref": "a"}, "right": {"ref": "b"}}).evaluate(
        ctx
    )
    assert not compile_condition(
        {"op": "lt", "left": {"ref": "a"}, "right": {"ref": "b"}}
    ).evaluate(ctx)


def test_gte_and_lte_are_inclusive() -> None:
    ctx = _ctx(indicators={"a": (Decimal("1"),), "b": (Decimal("1"),)})
    assert compile_condition({"op": "gte", "left": {"ref": "a"}, "right": {"ref": "b"}}).evaluate(
        ctx
    )
    assert compile_condition({"op": "lte", "left": {"ref": "a"}, "right": {"ref": "b"}}).evaluate(
        ctx
    )
    assert not compile_condition(
        {"op": "gt", "left": {"ref": "a"}, "right": {"ref": "b"}}
    ).evaluate(ctx)


def test_a_warming_up_indicator_makes_a_comparison_false() -> None:
    """`None` on either side is false, not an exception. No trade fires until every indicator a
    condition names has a value — the warm-up, enforced at the leaf."""
    ctx = _ctx(indicators={"a": (None,), "b": (Decimal("1"),)})
    assert not compile_condition(
        {"op": "gt", "left": {"ref": "a"}, "right": {"ref": "b"}}
    ).evaluate(ctx)


# --------------------------------------------------------------------------- #
# Edge operators                                                                #
# --------------------------------------------------------------------------- #


def test_crosses_above_is_true_only_on_the_bar_the_relation_flips() -> None:
    """fast was below slow last bar, above this bar. A `gt` would be true both this bar and
    every bar after; the cross is true exactly once."""
    node = {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}}
    condition = compile_condition(node)

    # this bar fast=2 > slow=1; last bar fast=0 <= slow=1  -> the flip
    flip = _ctx(
        indicators={"fast": (Decimal("2"), Decimal("0")), "slow": (Decimal("1"), Decimal("1"))}
    )
    assert condition.evaluate(flip)

    # already above on both bars -> not a cross, just a level that holds
    held = _ctx(
        indicators={"fast": (Decimal("3"), Decimal("2")), "slow": (Decimal("1"), Decimal("1"))}
    )
    assert not condition.evaluate(held)


def test_crosses_above_needs_two_full_bars() -> None:
    """On the first bar there is no previous value, so an edge cannot be true — the bar before
    the first one never exists."""
    node = {"op": "crosses_above", "left": {"ref": "fast"}, "right": {"ref": "slow"}}
    first_bar = _ctx(indicators={"fast": (Decimal("2"), None), "slow": (Decimal("1"), None)})
    assert not compile_condition(node).evaluate(first_bar)


def test_breaks_above_is_the_same_edge_as_crosses_above() -> None:
    """Guilherme's call (PR-104): a break is an edge event, mechanically identical to a cross
    in v1. This pins that so a future divergence is a deliberate change, caught by this test."""
    flip = _ctx(indicators={"a": (Decimal("2"), Decimal("0")), "b": (Decimal("1"), Decimal("1"))})
    crosses = compile_condition(
        {"op": "crosses_above", "left": {"ref": "a"}, "right": {"ref": "b"}}
    )
    breaks = compile_condition({"op": "breaks_above", "left": {"ref": "a"}, "right": {"ref": "b"}})
    assert crosses.evaluate(flip) == breaks.evaluate(flip) is True


def test_a_touch_then_break_counts_as_a_cross() -> None:
    """The boundary choice: `<=` on the previous bar, so equal-then-above is a cross. A
    deliberate, tested decision — not an accident of `<` versus `<=`."""
    node = {"op": "crosses_above", "left": {"ref": "a"}, "right": {"ref": "b"}}
    touched = _ctx(
        indicators={"a": (Decimal("2"), Decimal("1")), "b": (Decimal("1"), Decimal("1"))}
    )
    assert compile_condition(node).evaluate(touched)


def test_breakout_of_the_previous_high_uses_two_candles() -> None:
    """The canonical `breaks_above(price.high, candle[-1].high)`, resolved against real bars.

    This bar's high pierces last bar's high, having not pierced it the bar before.
    """
    node = {
        "op": "breaks_above",
        "left": {"ref": "price.high"},
        "right": {"ref": "candle[-1].high"},
    }
    condition = compile_condition(node)
    # candles newest-first: [now, prev, prev2]
    now = bar(2, open_="1.10", close="1.12", high="1.15", low="1.09")
    prev = bar(1, open_="1.09", close="1.10", high="1.11", low="1.08")
    prev2 = bar(0, open_="1.08", close="1.09", high="1.10", low="1.07")
    # at shift 0: price.high(now)=1.15 > candle[-1].high(prev)=1.11  ✓
    # at shift 1: price.high(prev)=1.11 <= candle[-1].high(prev2)=1.10?  1.11 <= 1.10 is False
    #   -> so the "was" side (prev_left <= prev_right) is False -> not a fresh break... adjust:
    assert condition.evaluate(_ctx((now, prev, prev2))) is False

    # Now make the previous bar not have broken: prev.high 1.09 <= prev2.high 1.10
    prev_low = bar(1, open_="1.085", close="1.09", high="1.095", low="1.08")
    # shift1: price.high(prev_low)=1.095 <= candle[-1].high(prev2)=1.10 ✓ (was below)
    # shift0: price.high(now)=1.15 > candle[-1].high(prev_low)=1.095 ✓ (now above) -> fresh break
    assert condition.evaluate(_ctx((now, prev_low, prev2))) is True


# --------------------------------------------------------------------------- #
# The tree                                                                      #
# --------------------------------------------------------------------------- #


def test_nested_all_any_not() -> None:
    """`all[ any[ a>b, not(c>d) ], e>f ]` — the shape of nested_logic.json, evaluated."""
    node = {
        "all": [
            {
                "any": [
                    {"op": "gt", "left": {"ref": "a"}, "right": {"ref": "b"}},
                    {"not": {"op": "gt", "left": {"ref": "c"}, "right": {"ref": "d"}}},
                ]
            },
            {"op": "gt", "left": {"ref": "e"}, "right": {"ref": "f"}},
        ]
    }
    condition = compile_condition(node)

    def ctx(**pairs: str) -> EvalContext:
        return _ctx(indicators={k: (Decimal(v),) for k, v in pairs.items()})

    # any-branch true via a>b; e>f true -> whole thing true
    assert condition.evaluate(ctx(a="2", b="1", c="1", d="1", e="2", f="1"))
    # a<=b AND c>d (so not(c>d) false) -> any false -> all false
    assert not condition.evaluate(ctx(a="1", b="2", c="2", d="1", e="2", f="1"))
    # any true (a>b) but e<=f -> all false
    assert not condition.evaluate(ctx(a="2", b="1", c="1", d="1", e="1", f="2"))


def test_an_unknown_operator_is_refused() -> None:
    with pytest.raises(EngineError, match="unknown comparison operator"):
        compile_condition({"op": "diverges", "left": {"ref": "a"}, "right": {"ref": "b"}})


def test_an_unrecognised_node_is_refused() -> None:
    with pytest.raises(EngineError, match="unrecognised condition node"):
        compile_condition({"maybe": []})


def test_a_comparison_operand_with_neither_ref_nor_value_is_refused() -> None:
    with pytest.raises(EngineError, match="needs a 'ref' or a 'value'"):
        compile_condition({"op": "gt", "left": {"threshold": 1}, "right": {"ref": "b"}})


def test_a_comparison_operand_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(EngineError, match="operand is not an object"):
        compile_condition({"op": "gt", "left": 30, "right": {"ref": "b"}})


def test_a_ref_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(EngineError, match="ref must be a string"):
        compile_condition({"op": "gt", "left": {"ref": 1}, "right": {"ref": "b"}})


def test_a_not_that_is_not_a_condition_is_refused() -> None:
    with pytest.raises(EngineError, match="'not' takes one condition"):
        compile_condition({"not": [1, 2]})


def test_an_empty_logic_list_is_refused() -> None:
    with pytest.raises(EngineError, match="non-empty list"):
        compile_condition({"all": []})


def test_a_logic_child_that_is_not_a_condition_is_refused() -> None:
    with pytest.raises(EngineError, match="child is not a condition"):
        compile_condition({"any": ["not a node"]})
