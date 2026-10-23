"""Every average a setup names carries the floor — found by reflection, not by a list.

His call, 2026-09-10: no setup may be built on an average shorter than three bars. The floor is
seven fields today, and a fixture can only ever prove one of them. What matters is not those seven
but the next one: the VWAP setups are in the queue, and a new params model with `ge=1` typed out of
habit would slip past a suite that only tested `mme9_breakout`.

⚠️ **This is the guard that makes the floor an invariant instead of a coincidence.** The failure it
prevents is silent by construction: an exponential average of one period *is* the close, so
`mme9_breakout` — whose rule is a bar closing across the average — can never arm, and the document
is accepted, the backtest runs, and the report is zero trades with nothing complaining.

So the test walks the discriminated union, reaches into each member's params model, and asserts on
every field whose name mentions a period. Adding a setup adds it here with no edit; adding a
period-shaped field to an existing one does the same.
"""

from typing import Annotated, Any, get_args, get_origin

import pytest

from tradeforge_schema.models import AVERAGE_FLOOR, Setup


def _setup_members() -> tuple[type[Any], ...]:
    """The setup nodes inside `Setup`, which is a PEP-695 alias over an `Annotated` union."""
    inner = Setup.__value__
    assert get_origin(inner) is Annotated, f"Setup stopped being Annotated: {inner!r}"
    return get_args(get_args(inner)[0])


def _period_fields() -> list[tuple[str, str, Any]]:
    """Every `(setup type, field name, field)` in the DSL whose name mentions a period."""
    found: list[tuple[str, str, Any]] = []
    for member in _setup_members():
        kind = get_args(member.model_fields["type"].annotation)[0]
        params = member.model_fields["params"].annotation
        for name, field in params.model_fields.items():
            if "period" in name:
                found.append((kind, name, field))
    return found


def test_the_union_is_reachable_and_carries_every_setup() -> None:
    """The reflection above is the whole test, so it has to fail loudly if it stops reaching.

    A `_period_fields()` that quietly returned nothing would make the assertion below vacuous —
    the classic green test over an empty list.
    """
    kinds = {kind for kind, _, _ in _period_fields()}

    assert len(_setup_members()) >= 5
    assert {
        "mme9_breakout",
        "mme9_turn",
        "mme9_pullback",
        "mme9_failed_turn",
        "ponto_continuo",
    } <= (kinds)


@pytest.mark.parametrize(
    ("kind", "name", "field"),
    [(kind, name, field) for kind, name, field in _period_fields()],
    ids=[f"{kind}.{name}" for kind, name, _ in _period_fields()],
)
def test_every_setup_average_starts_at_the_floor(kind: str, name: str, field: Any) -> None:
    floors = [getattr(meta, "ge", None) for meta in field.metadata]

    assert AVERAGE_FLOOR in floors, (
        f"{kind}.{name} does not start at {AVERAGE_FLOOR}. A setup's average is his rule, not this "
        f"field's own business — see AVERAGE_FLOOR"
    )
