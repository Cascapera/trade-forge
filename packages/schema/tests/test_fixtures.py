"""Every fixture, through both layers of validation.

The three directories are the point of this test file:

* `valid/`            — passes shape *and* meaning.
* `invalid-schema/`   — malformed. Rejected by the schema, so both Python and the
                        frontend catch it.
* `invalid-semantic/` — well-formed and unrunnable. The schema accepts it; only the
                        Python layer knows better. The tests below *assert* that the
                        schema accepts them, which pins the boundary in place: nobody
                        gets to assume a document that passed in the browser is
                        executable.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradeforge_schema.models import Strategy
from tradeforge_schema.semantic import validate_semantics

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# What each malformed document should be rejected *for*. Asserting the message, not
# just the failure, is what keeps a fixture from passing for the wrong reason.
SCHEMA_ERRORS = {
    "unknown_operator.json": "greater_than",
    "zero_candle_offset.json": "candle[-0].high",
    "misspelled_param.json": "perod",
}

SEMANTIC_ERRORS = {
    "undeclared_indicator.json": "undeclared indicator 'sma_slow'",
    "take_profit_without_stop.json": "no risk to multiply",
    "no_entry_side.json": "at least one side",
}


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _fixtures(group: str) -> list[Path]:
    return sorted((FIXTURES / group).glob("*.json"))


@pytest.mark.parametrize("path", _fixtures("valid"), ids=lambda p: p.name)
def test_valid_fixtures_pass_both_layers(path: Path) -> None:
    strategy = Strategy.model_validate(_load(path))

    assert validate_semantics(strategy) == []


@pytest.mark.parametrize("path", _fixtures("invalid-schema"), ids=lambda p: p.name)
def test_malformed_fixtures_are_rejected_by_the_schema(path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        Strategy.model_validate(_load(path))

    assert SCHEMA_ERRORS[path.name] in str(caught.value)


@pytest.mark.parametrize("path", _fixtures("invalid-semantic"), ids=lambda p: p.name)
def test_unrunnable_fixtures_pass_the_schema_and_fail_on_meaning(path: Path) -> None:
    """The whole reason `semantic.py` exists, stated as a test."""
    # Shape: fine. This is not an oversight — it is the limit of what a schema *can* say.
    strategy = Strategy.model_validate(_load(path))

    errors = validate_semantics(strategy)

    assert errors, "a schema-valid but unrunnable strategy must be caught by the semantic layer"
    assert SEMANTIC_ERRORS[path.name] in "; ".join(str(error) for error in errors)


def test_every_fixture_is_covered_by_an_expectation() -> None:
    """A fixture nobody asserts on is a file, not a test."""
    assert {p.name for p in _fixtures("invalid-schema")} == set(SCHEMA_ERRORS)
    assert {p.name for p in _fixtures("invalid-semantic")} == set(SEMANTIC_ERRORS)
    assert len(_fixtures("valid")) >= 3
