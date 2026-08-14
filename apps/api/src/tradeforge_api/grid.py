"""Expanding a grid of parameter ranges into the concrete strategy documents to run.

A study asks a different question from a backtest. One run says *"these parameters returned
18%"*; a grid says *"here is how the result behaves across the space of parameters"* — and only
the second can tell a method apart from a lucky corner. So the unit of work is still one
`Backtest` over one document, and everything here is about producing those documents honestly.

Pure on purpose: no database, no HTTP, no engine. Expanding a grid is arithmetic over a
dictionary, and arithmetic that needs a Postgres container to be tested is arithmetic nobody
tests at the edges. The router does the writing; this module decides what is to be written.

**Each point becomes its own stored strategy**, never an override carried on the run. A backtest
points at a `strategy_id` and that document is what it executed — reproducing a run is reading
one row. An override column would make "what did this run actually execute" a question with two
answers, joined by merge logic that could drift, and nothing would raise when it did.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

type Json = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
"""What a strategy document is made of, spelled out.

Named rather than left as `Any` because every function here *walks* a document — descending
through dicts and lists, deciding at each step whether there is anywhere further to go — and
bare `Any` in those signatures would switch the type checker off across exactly the code whose
job is reaching into a structure it does not control.

⚠️ **A grid's values are deliberately not typed this tightly** (`Sequence[Any]` below). They are
never inspected here: a value is copied into a document and handed to the DSL validator, which
is the component that owns what a parameter may be. Narrowing them would buy no checking that
the validator does not already do, and would cost every caller a cast to say so.
"""

MAX_POINTS = 500
"""How many combinations one study will expand to.

A cap and not a sample: a grid too large is refused with its own size in the message, never
quietly trimmed. Half a grid drawn as a heatmap is a picture of a space that was never searched,
and it looks exactly like a picture of one that was.

The number is a budget rather than a technical limit. Measured on this project's own runs, a
backtest takes 0.1 to 0.8 seconds, so 500 points is a study of a few minutes — long enough to
be a deliberate act, short enough to sit through. The queue would happily take more; the reason
not to is on `Study` in the models, and it is about what a grid this wide does to a conclusion.
"""


class GridError(ValueError):
    """A grid that cannot be expanded into documents anyone should run."""


@dataclass(frozen=True, slots=True)
class GridPoint:
    """One combination: the values chosen, and the document they produce."""

    values: Mapping[str, Any]
    """Path to chosen value, e.g. `{"setup.params.period": 9}`. Ordered as the grid was given,
    so a study's points read in the order the axes were declared rather than in hash order."""

    document: dict[str, Any]
    """The base strategy with those values substituted in. Validated by the caller before use.

    `dict[str, Any]`, not `dict[str, Json]`, and the looseness is on purpose: this is what the
    rest of the project calls a strategy document (`schemas.StrategyOut.definition`, the ORM's
    `Strategy.definition`), and it goes straight into one of those. `Json` earns its keep inside
    the walking helpers, where every `isinstance` is a real narrowing; at the boundary it would
    only make every reader destructure a union it already knows the shape of.
    """

    @property
    def label(self) -> str:
        """A human-readable name for this point. See `label_for`."""
        return label_for(self.values)


def label_for(values: Mapping[str, Any]) -> str:
    """What makes a point different from the others: `period=9, take_profit_rr=3`.

    Only the leaf of each path, because the prefix is identical across every point of a study
    and repeating it would push the part that differs off the end of a column.

    Shared by the two places a point is described — the body that creates a study and the body
    that reads one back — because they were built independently once and disagreed: one served
    this, the other served the stored strategy's full name. One field, two formats, and only a
    client trying to match them would ever find out.
    """
    return ", ".join(f"{path.rsplit('.', 1)[-1]}={value!r}" for path, value in values.items())


def _walk(document: Json, path: str) -> tuple[Json, str]:
    """Resolve `path` against `document`, returning the container and the final key.

    Numeric segments index into lists, so `indicators.0.params.period` reaches a DSL document's
    first indicator. Anything unresolvable raises — see `expand` for why that has to be loud.

    ⚠️ **The last segment is resolved too, and its result thrown away.** Stopping at the
    second-to-last would return a container for any path whose prefix happens to exist, and two
    different mistakes would slip through: `setup.type.period`, where the walk lands *inside a
    string* and there is no container at all, and `setup.params.periodd`, where the container is
    real and the key simply is not in it. The second is the dangerous one — it is what a typo
    looks like, and substituting there would add a parameter nothing reads.
    """
    parts = path.split(".")
    if not path or any(not part for part in parts):
        raise GridError(f"{path!r} is not a path into a strategy document")

    current: Json = document
    for depth, part in enumerate(parts[:-1]):
        current = _descend(current, part, path, depth)
    _descend(current, parts[-1], path, len(parts) - 1)
    return current, parts[-1]


def _descend(current: Json, part: str, path: str, depth: int) -> Json:
    """One step along a path, refusing anything that does not lead somewhere."""
    where = ".".join(path.split(".")[: depth + 1])
    if isinstance(current, list):
        if not part.isdigit() or int(part) >= len(current):
            raise GridError(f"{path!r}: {where!r} is a list of {len(current)}, not {part!r}")
        return current[int(part)]
    if not isinstance(current, dict) or part not in current:
        raise GridError(f"{path!r}: this strategy has nothing at {where!r}")
    return current[part]


def _set(document: dict[str, Any], path: str, value: Any) -> None:  # noqa: ANN401
    """Substitute `value` at `path` in an already-copied document."""
    container, key = _walk(document, path)
    if isinstance(container, list):
        container[int(key)] = value
    elif isinstance(container, dict):
        container[key] = value
    else:  # pragma: no cover — `_walk` resolves the last segment, so this cannot be reached
        # Kept rather than dropped as dead code: `_walk` is what makes it unreachable, and if
        # that guarantee ever weakens this is the difference between a loud refusal and a
        # substitution that silently does nothing while the study reports N identical runs.
        raise GridError(f"{path!r} does not name a place a value can be put")


def _copy(value: Json) -> Json:
    """Deep-copy the parts of a JSON document a substitution could reach.

    Written out rather than `copy.deepcopy` because a strategy document is JSON — dicts, lists
    and scalars — and the shallow copy that would be a bug here is precisely the one where two
    points end up sharing a nested `params` dict. Sharing it would make the last substitution
    win for every point, and the study would run the same document N times under N names.
    """
    if isinstance(value, dict):
        return {key: _copy(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_copy(inner) for inner in value]
    return value


def _check_axes(document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]) -> None:
    """Refuse a grid before expanding it — every axis reachable, populated, and not constant."""
    if not grid:
        raise GridError("a study needs at least one parameter to vary")

    for path, values in grid.items():
        # ⚠️ The check that matters most, and the only one whose absence is silent. A path this
        # document has nothing at — `setup.params.periodd` for a typo, or an axis meant for a
        # different strategy — would simply *add* a key. Nothing reads it: the setup factory
        # takes the parameters it knows and ignores the rest. The study would then run N
        # identical backtests, produce N identical results, and draw a perfectly flat heatmap
        # that looks like a finding about the market.
        _walk(dict(document), path)
        if not values:
            raise GridError(f"{path!r} has no values to try")
        if len(set(map(repr, values))) != len(values):
            raise GridError(f"{path!r} repeats a value; each point of a grid must be distinct")


def size_of(grid: Mapping[str, Sequence[Any]]) -> int:
    """How many points this grid expands to, without building any of them.

    Separate from `expand` because the number is what a caller needs *before* committing: the
    product grows multiplicatively, so a fourth axis of five values is not 5 more points but 5
    times as many, and that is not obvious while typing the fourth row of a form.
    """
    total = 1
    for values in grid.values():
        total *= len(values)
    return total


def expand(
    document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]], *, limit: int = MAX_POINTS
) -> list[GridPoint]:
    """Every combination of the grid, as its own strategy document.

    Raises `GridError` — never a partial expansion — if any axis is unreachable, empty or
    repeats a value, or if the product exceeds `limit`.

    The cartesian product is taken in the order the axes were declared, with the **last axis
    varying fastest**, which is `itertools.product`'s own order and the one a reader expects
    from a nested loop. It matters because that order becomes the order of the runs in the
    study, and a heatmap laid out row by row is reading it.
    """
    _check_axes(document, grid)

    total = size_of(grid)
    if total > limit:
        raise GridError(
            f"this grid expands to {total} combinations, over the {limit} a study will run"
        )

    return [
        GridPoint(
            values=dict(zip(grid, chosen, strict=True)), document=_apply(document, grid, chosen)
        )
        for chosen in product(*grid.values())
    ]


def _apply(
    document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]], chosen: Sequence[Any]
) -> dict[str, Any]:
    """One point's document: a fresh deep copy of the base with each axis substituted."""
    built = {key: _copy(value) for key, value in document.items()}
    for path, value in zip(grid, chosen, strict=True):
        _set(built, path, value)
    return built


def value_at(document: Mapping[str, Any], path: str) -> Any:  # noqa: ANN401
    """The value a document holds at `path`, raising `GridError` if it holds nothing there.

    The inverse of the substitution, and it exists so a reader can recover which point a stored
    document *is* without parsing the name it was given. A heatmap has to place each run on its
    axes; doing that from a caption means splitting on commas and equals signs, which works
    until a value contains one.
    """
    container, key = _walk(dict(document), path)
    if isinstance(container, list):
        return container[int(key)]
    if isinstance(container, dict):
        return container[key]
    # pragma: no cover — `_walk` resolves the last segment, so the container is always one of
    # the two above. Kept loud for the same reason `_set`'s twin is.
    raise GridError(f"{path!r} does not name a place a value can be read from")


def named(base_name: str, point: GridPoint) -> str:
    """The stored name for a point's strategy: the base name plus what makes it different.

    `MME9 breakout [period=9, take_profit_rr=3]`. The bracketed part is the whole reason this
    exists rather than letting the points be versions 1..N of one lineage: they *are* stored as
    separate rows either way, and a run log row reading `MME9 breakout v37` cannot be read at
    all — the run log is exactly where a study's result gets read.
    """
    return f"{base_name} [{point.label}]"
