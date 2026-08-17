"""Semantic rules, one at a time."""

from typing import Any

import pytest

from tradeforge_schema.generate import main
from tradeforge_schema.models import Strategy
from tradeforge_schema.semantic import (
    SemanticValidationError,
    assert_executable,
    validate_semantics,
)


def strategy(**overrides: object) -> Strategy:
    """A minimal runnable strategy, with the parts under test swapped in."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "base",
        "timeframe": "H1",
        "indicators": [{"id": "sma", "type": "SMA", "params": {"period": 9, "source": "close"}}],
        "entry": {"long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "sma"}}},
        "exit": {"stop_loss": {"type": "candle_extreme", "params": {"lookback": 1, "side": "low"}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    document.update(overrides)
    return Strategy.model_validate(document)


def messages(model: Strategy) -> str:
    return "; ".join(str(error) for error in validate_semantics(model))


def test_the_base_strategy_is_executable() -> None:
    assert validate_semantics(strategy()) == []


def test_a_constant_operand_needs_no_indicator_declaration() -> None:
    """`rsi < 30` compares an indicator against a literal. The constant references nothing, so the
    semantic layer must not mistake it for an undeclared indicator (and RSI is a valid block)."""
    model = strategy(
        indicators=[{"id": "rsi", "type": "RSI", "params": {"period": 14}}],
        entry={"long": {"op": "lt", "left": {"ref": "rsi"}, "right": {"value": 30}}},
    )
    assert validate_semantics(model) == []


def test_duplicate_indicator_ids_are_rejected() -> None:
    """Two indicators with one id: every ref to it becomes ambiguous."""
    model = strategy(
        indicators=[
            {"id": "sma", "type": "SMA", "params": {"period": 9}},
            {"id": "sma", "type": "EMA", "params": {"period": 21}},
        ],
    )

    assert "duplicate indicator id 'sma'" in messages(model)


def test_an_indicator_may_not_take_a_reserved_name() -> None:
    """An indicator called `price` would make `price.close` mean two things."""
    model = strategy(
        indicators=[{"id": "price", "type": "SMA", "params": {"period": 9}}],
        entry={"long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "price"}}},
    )

    assert "reserved namespace" in messages(model)


def test_refs_are_checked_deep_inside_the_expression_tree() -> None:
    """A bad ref buried under all > any > not must still be found."""
    model = strategy(
        entry={
            "long": {
                "all": [
                    {
                        "any": [
                            {"not": {"op": "lt", "left": {"ref": "ghost"}, "right": {"ref": "sma"}}}
                        ]
                    },
                ],
            },
        },
    )

    assert (
        "entry.long.all[0].any[0].not.left: reference to undeclared indicator 'ghost'"
        in messages(
            model,
        )
    )


def test_price_and_candle_refs_need_no_declaration() -> None:
    model = strategy(
        indicators=[],
        entry={
            "long": {
                "op": "breaks_above",
                "left": {"ref": "price.high"},
                "right": {"ref": "candle[-2].high"},
            },
        },
    )

    assert validate_semantics(model) == []


# --------------------------------------------------------------------------- #
# Multi-output indicators: the component half of the ref grammar                 #
# --------------------------------------------------------------------------- #


def bands(**overrides: object) -> Strategy:
    """A strategy whose only indicator is a Bollinger, so refs to it can be varied."""
    document: dict[str, object] = {
        "indicators": [{"id": "bb", "type": "BOLLINGER", "params": {"period": 20}}],
        "entry": {
            "long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "bb.upper"}},
        },
    }
    document.update(overrides)
    return strategy(**document)


def test_a_component_of_a_declared_multi_output_indicator_resolves() -> None:
    assert validate_semantics(bands()) == []


def test_every_component_a_composite_answers_to_is_accepted() -> None:
    """All three bands, and all three ADX lines — the mapping is the contract, not one entry."""
    for component in ("upper", "middle", "lower"):
        model = bands(
            entry={
                "long": {
                    "op": "gt",
                    "left": {"ref": "price.close"},
                    "right": {"ref": f"bb.{component}"},
                },
            },
        )
        assert validate_semantics(model) == []

    for component in ("adx", "plus_di", "minus_di"):
        model = strategy(
            indicators=[{"id": "trend", "type": "ADX", "params": {"period": 14}}],
            entry={
                "long": {
                    "op": "gt",
                    "left": {"ref": f"trend.{component}"},
                    "right": {"value": 25},
                },
            },
        )
        assert validate_semantics(model) == []


def test_a_misspelled_component_is_refused_rather_than_read_as_nothing() -> None:
    """⚠️ The regression this whole layer exists to prevent, and it is the silent kind.

    Widening `REF_PATTERN` to accept `id.component` also made `bb.uppper` well-formed. The old
    test for "is this an indicator ref" asked whether the string contained a dot — so a typo would
    have sailed through here, reached the engine, resolved to `None` for want of a channel by that
    name, and made its condition **false on every bar of every run**. Nothing raises, nothing logs,
    and the backtest reports zero trades as though the market never qualified.
    """
    model = bands(
        entry={
            "long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "bb.uppper"}},
        },
    )

    assert "has no component 'uppper'" in messages(model)
    assert "middle, upper, lower" in messages(model)


def test_a_multi_output_indicator_cannot_be_referenced_bare() -> None:
    """No default component: `bb` alone is a question with three answers.

    "The middle band" is the tempting default, and there is no defensible one for ADX at all. A
    default would also make `bb` and `bb.middle` two spellings of one value, which `REF_PATTERN`'s
    own comment gives as how a DSL starts to rot.
    """
    model = bands(
        entry={"long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "bb"}}},
    )

    assert "has several outputs" in messages(model)
    # In the order the components are declared, which is primary first — so the message reads as
    # a suggestion ("did you mean the average?") rather than as an alphabetical dump.
    assert "bb.middle, bb.upper, bb.lower" in messages(model)


def test_a_single_valued_indicator_cannot_be_referenced_by_component() -> None:
    """The mirror image, and it fails the same silent way if unchecked: `sma.upper` names a channel
    the compiler never opens, so the comparison is false for ever."""
    model = strategy(
        entry={"long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "sma.upper"}}},
    )

    assert "answers with a single value" in messages(model)
    assert "reference it as 'sma'" in messages(model)


def test_a_misspelled_price_field_is_refused() -> None:
    """The second half of the same hole, and the one that got past the first fix.

    `price.clsoe` is well-formed under the component alternative — `price` is a fine identifier and
    `clsoe` a fine component name — so widening the grammar for `bb.upper` quietly made a
    misspelling of the DSL's most-typed ref legal. The engine has no channel by that name, resolves
    it to `None`, and the condition is false on every bar of every run: the rule reports as declined
    rather than as broken, and nothing anywhere says a word.

    Caught here rather than by the pattern, and not by choice. Pydantic compiles `pattern=` with
    Rust's `regex` crate, which has no look-around at all, so "an identifier that is not `price`"
    cannot be stated there — the negative lookahead refuses to compile and takes the package's
    import with it. Which lands the check exactly where this file's subject is: a schema states
    shape, and this is meaning.
    """
    for bad in ("price.clsoe", "price.volume"):
        model = bands(
            entry={"long": {"op": "gt", "left": {"ref": bad}, "right": {"ref": "bb.upper"}}},
        )
        assert "is not a price field" in messages(model), bad
        assert "price.close" in messages(model), bad

    bare = bands(
        entry={"long": {"op": "gt", "left": {"ref": "candle.high"}, "right": {"ref": "bb.upper"}}},
    )
    assert "must name a closed candle and a field" in messages(bare)


def test_the_legal_price_and_candle_forms_are_still_legal() -> None:
    """The other half of the pair, and it is not decoration.

    A check that refused `price.close` along with the typo would be worse than the hole it closed,
    and "refuses the misspelling" on its own passes just as happily on a check that refuses
    everything. Same shape as [[golden-separa-do-antigo-nao-do-vizinho]]: the test that proves the
    fix has to exclude the neighbour it must not break.
    """
    for good in ("price.close", "price.open", "price.high", "price.low", "candle[-2].high"):
        model = bands(
            entry={"long": {"op": "gt", "left": {"ref": good}, "right": {"ref": "bb.upper"}}},
        )
        assert validate_semantics(model) == [], good


def test_an_undeclared_composite_component_names_the_indicator_not_the_channel() -> None:
    """The error has to point at `ghost`, not at `ghost.upper` — the missing thing is the
    declaration, and a message naming the channel would send someone hunting for a component."""
    model = bands(
        entry={
            "long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"ref": "ghost.upper"}},
        },
    )

    assert "reference to undeclared indicator 'ghost'" in messages(model)


def test_exit_conditions_are_checked_too() -> None:
    model = strategy(
        exit={
            "stop_loss": {"type": "candle_extreme", "params": {"lookback": 1, "side": "low"}},
            "conditions": [{"op": "lt", "left": {"ref": "price.close"}, "right": {"ref": "ghost"}}],
        },
    )

    assert "exit.conditions[0].right" in messages(model)


def test_assert_executable_raises_with_every_reason_at_once() -> None:
    """One round-trip, all the problems — not a game of whack-a-mole for the user."""
    model = strategy(
        indicators=[],
        entry={"long": None, "short": None},
        exit={"stop_loss": None, "take_profit": {"type": "risk_multiple", "params": {"rr": 2.0}}},
    )

    with pytest.raises(SemanticValidationError) as caught:
        assert_executable(model)

    assert len(caught.value.errors) == 2
    assert "at least one side" in str(caught.value)
    assert "no risk to multiply" in str(caught.value)


def test_assert_executable_returns_the_strategy_when_sound() -> None:
    model = strategy()

    assert assert_executable(model) is model


def test_generator_writes_the_schema_where_consumers_expect_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main() == 0
    assert "strategy.schema.json" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Setup documents: the other shape a strategy can have (ADR-0019)               #
# --------------------------------------------------------------------------- #


def setup_strategy(**overrides: object) -> Strategy:
    """A minimal runnable *setup* document: it names a strategy instead of describing one."""
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "base setup",
        "timeframe": "H1",
        "setup": {"type": "ponto_continuo", "params": {"side": "long"}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    document.update(overrides)
    return Strategy.model_validate(document)


def test_a_setup_document_needs_no_entry_conditions() -> None:
    """The rule that a strategy must trade at least one side does not apply here: the setup *is*
    the entry, and which side it takes is its own parameter (or, for the structure family, the
    structure's business). Applying it would reject every setup document there is."""
    assert validate_semantics(setup_strategy()) == []


def test_a_setup_may_name_a_target_without_declaring_a_stop() -> None:
    """The interaction that is easiest to get wrong. A risk-multiple target needs a risk to
    multiply, and for a condition strategy that risk comes from `exit.stop_loss` — hence the rule.
    A setup places its own stop, from the bar it entered on, so the risk exists without the field
    ever appearing. Enforcing the rule here would reject exactly the documents worth running: the
    author trades this at five times the risk.
    """
    model = setup_strategy(exit={"take_profit": {"type": "risk_multiple", "params": {"rr": 5.0}}})

    assert validate_semantics(model) == []


def test_a_setup_cannot_be_combined_with_entry_conditions() -> None:
    """Refused, not ignored. Both would be an entry decision with no arbiter, and silently
    dropping the one the user wrote is how a backtest answers a question nobody asked."""
    model = setup_strategy(
        entry={"long": {"op": "gt", "left": {"ref": "price.close"}, "right": {"value": 100}}}
    )

    assert "cannot be combined with entry conditions" in messages(model)


def test_a_setup_declares_its_own_indicators() -> None:
    model = setup_strategy(
        indicators=[{"id": "sma", "type": "SMA", "params": {"period": 9, "source": "close"}}]
    )

    assert "a setup declares its own indicators" in messages(model)


def test_a_setup_cannot_be_given_a_stop_rule() -> None:
    model = setup_strategy(
        exit={"stop_loss": {"type": "candle_extreme", "params": {"lookback": 1, "side": "low"}}}
    )

    assert "places its own stop" in messages(model)


def test_a_setup_cannot_be_given_exit_conditions() -> None:
    """A setup leaves at a level — its conducted stop or the broker's target — never on a rule
    evaluated at a close. An exit condition would be a third way out that nothing else knows."""
    model = setup_strategy(
        exit={"conditions": [{"op": "lt", "left": {"ref": "price.close"}, "right": {"value": 100}}]}
    )

    assert "conducts its own exit" in messages(model)


def test_a_document_that_is_neither_a_setup_nor_a_strategy_is_refused() -> None:
    """Dropping `entry` from a document with no `setup` leaves nothing that decides anything. The
    existing rule catches it, and this pins that making `entry` optional did not open a hole."""
    model = strategy(entry={})

    assert "at least one side" in messages(model)
