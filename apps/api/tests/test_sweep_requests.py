"""What a sweep request refuses before any database is asked.

The integration suite proves what a sweep writes; this file proves what never gets that far. A
repeated value on any axis is refused by the schema itself, so the router — and the `(name,
version)` collision, and the doubled runs — are never reached.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from tradeforge_api.schemas import CreateSweep, PreviewSweepRequest

# Fixed, not `uuid4()`: the value lands in the parametrised test ids, and an id that changes on
# every collection cannot be re-run by name.
ENTRY = "0f3c9a52-6d1e-4b7a-9c2d-3e4f5a6b7c8d"
OTHER = "7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def a_preview(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "entry_ids": [ENTRY, OTHER],
        "symbols": ["EURUSD", "GBPUSD"],
        "timeframes": ["M15", "H1"],
        "date_from": "2024-01-01T00:00:00Z",
        "date_to": "2024-06-01T00:00:00Z",
    }
    return {**body, **over}


def a_launch(**over: Any) -> dict[str, Any]:
    return {**a_preview(), "initial_capital": "10000", "cost_model": {"type": "none"}, **over}


SCHEMAS = [
    pytest.param(PreviewSweepRequest, a_preview, id="preview"),
    pytest.param(CreateSweep, a_launch, id="launch"),
]


@pytest.mark.parametrize(("schema", "body"), SCHEMAS)
def test_distinct_axes_are_accepted(schema: Any, body: Any) -> None:
    # The control: without it every refusal below could be a fixture the schema rejects for
    # some other reason, and the suite would read as green for the wrong one.
    schema.model_validate(body())


@pytest.mark.parametrize(("schema", "body"), SCHEMAS)
@pytest.mark.parametrize(
    ("field", "repeated", "named"),
    [
        ("entry_ids", [ENTRY, OTHER, ENTRY], ENTRY),
        ("symbols", ["EURUSD", "GBPUSD", "EURUSD"], "EURUSD"),
        ("timeframes", ["M15", "H1", "M15"], "M15"),
    ],
)
def test_a_repeated_value_is_refused_and_named(
    schema: Any, body: Any, field: str, repeated: list[str], named: str
) -> None:
    # ⚠️ Both endpoints, because the preview promises what the launch delivers: a preview that
    # accepted `[A, A]` would count twice the runs the launch then refuses to create.
    with pytest.raises(ValidationError) as caught:
        schema.model_validate(body(**{field: repeated}))

    (error,) = caught.value.errors()
    assert error["loc"] == (field,)
    # The value is named, so a caller with a long list is told which one, not merely that one is.
    assert f"{field} must be distinct; repeated: {named}" in error["msg"]
