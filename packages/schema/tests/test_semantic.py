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
