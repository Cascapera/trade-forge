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
