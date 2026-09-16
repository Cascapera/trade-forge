"""The name a generated point is stored under, and the limit it has to fit.

`fit_name` exists because of a sweep that produced nothing: a five-axis entry expanded to 120
documents and every one was refused for `name: String should have at most 120 characters`. The
strategy was valid; the caption was nine characters too long.

What these tests hold is the pair of properties that make trimming safe — the result fits, and
two points never end up with the same name — plus the one that makes it reusable: the same point
gets the same name every time, in every process.
"""

from typing import Any

from tradeforge_api.grid import fit_name
from tradeforge_schema import NAME_MAX_LENGTH

BASE = "PC DE COMPRA CLASSICO"
LONG_LABEL = (
    "H4 · side='long', entry_point='classic', volume_filter=True, "
    "breakeven_at_r=None, long_average_period=200"
)


def values(**over: Any) -> dict[str, Any]:
    point: dict[str, Any] = {
        "timeframe": "H4",
        "setup.params.side": "long",
        "setup.params.entry_point": "classic",
        "setup.params.volume_filter": True,
        "setup.params.breakeven_at_r": None,
        "setup.params.long_average_period": 200,
    }
    point.update({f"setup.params.{key}": value for key, value in over.items()})
    return point


def test_a_name_that_already_fits_is_left_exactly_as_it_was() -> None:
    # The common case is a grid of one or two axes, and those names must not change: they are
    # what the run log has been showing, and a rename would rewrite history for no reason.
    assert fit_name("MME9 breakout", "period=9", {"setup.params.period": 9}) == (
        "MME9 breakout [period=9]"
    )


def test_a_name_that_does_not_fit_is_trimmed_to_the_limit() -> None:
    name = fit_name(BASE, LONG_LABEL, values())

    assert len(f"{BASE} [{LONG_LABEL}]") > NAME_MAX_LENGTH, "the fixture stopped being too long"
    assert len(name) <= NAME_MAX_LENGTH
    assert name.startswith(f"{BASE} [")
    assert name.endswith("]")
    assert "…" in name, "a trimmed name has to say it was trimmed"


def test_a_name_that_lands_exactly_on_the_limit_is_not_trimmed() -> None:
    # ⚠️ The boundary, and it is the one an off-by-one lives on: `<` instead of `<=` would trim a
    # name that fits perfectly, and every test above it would still pass.
    base = "B" * 90
    label = "x" * (NAME_MAX_LENGTH - len(base) - 3)

    name = fit_name(base, label, {"setup.params.period": 9})

    assert len(name) == NAME_MAX_LENGTH
    assert name == f"{base} [{label}]"
    assert "…" not in name


def test_the_digest_is_the_same_one_a_later_process_will_compute() -> None:
    """⚠️ **The test above cannot see the hazard this one exists for.** Calling `fit_name` twice
    in one process agrees even if the digest came from the built-in `hash()` — which is salted
    per process, so tomorrow's run would name the same point differently and store a second
    strategy instead of finding the first. A fixed expectation is what pins sha256.

    The digest and the length are asserted rather than the whole 120-character line: the point is
    the value, and a transcription of the rest would break on a cosmetic change to the label.
    """
    name = fit_name(BASE, LONG_LABEL, values())

    assert name.endswith("… #cddd7e]")
    assert len(name) == NAME_MAX_LENGTH
    assert name.startswith(f"{BASE} [H4 · side='long'")


def test_two_points_that_differ_only_at_the_end_get_different_names() -> None:
    # ⚠️ The property trimming exists to protect. A grid's points differ in their **last** axis,
    # so everything a trim keeps is identical between neighbours — and `(name, version)` is
    # unique, so the second one would be refused by the database rather than run.
    first = fit_name(BASE, LONG_LABEL, values(long_average_period=50))
    second = fit_name(BASE, LONG_LABEL, values(long_average_period=200))

    assert first != second
    assert len(first) <= NAME_MAX_LENGTH
    assert len(second) <= NAME_MAX_LENGTH


def test_the_same_point_is_named_the_same_way_every_time() -> None:
    # ⚠️ Determinism is not cosmetic here: `strategies_for` finds an existing point by its
    # document, and a name that changed between runs would write a second row instead.
    assert fit_name(BASE, LONG_LABEL, values()) == fit_name(BASE, LONG_LABEL, values())


def test_two_points_whose_values_differ_beyond_the_trim_are_still_told_apart() -> None:
    # The digest is over the values, not over the visible text, so it separates points whose
    # difference the trim cut off entirely.
    long_base = "X" * (NAME_MAX_LENGTH - 10)

    first = fit_name(long_base, LONG_LABEL, values(side="long"))
    second = fit_name(long_base, LONG_LABEL, values(side="short"))

    assert first != second
    assert max(len(first), len(second)) <= NAME_MAX_LENGTH


def test_a_base_name_that_alone_overflows_still_yields_a_usable_name() -> None:
    # A shelf label may be up to 120 characters by itself, which leaves nothing for a label.
    name = fit_name("Y" * NAME_MAX_LENGTH, LONG_LABEL, values())

    assert 0 < len(name) <= NAME_MAX_LENGTH
    assert name.endswith("]")
