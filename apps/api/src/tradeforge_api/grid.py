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

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

from tradeforge_schema import NAME_MAX_LENGTH

type Json = str | int | float | bool | list["Json"] | dict[str, "Json"] | None
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

# ⚠️ **No cap on the size of a grid** (his decision, 18/09). There was one — 500 — justified as a
# time budget at 0.23 s a run; measured on his own sweep the same day, a run of M30 over six years
# takes about 43 s. The number was wrong, and the reason was wrong too: he wants every variation
# of the method searched and will wait hours for it. How long a grid takes is something to show,
# not a ground to refuse. What is still refused is a grid that cannot be run at all — an empty or
# unreachable axis, a repeated value — because those are not sizes.


TAKE_PROFIT = "exit.take_profit"
"""Where the target lives in a document — a **block**, or `null` for no target at all."""

TAKE_PROFIT_RR = f"{TAKE_PROFIT}.params.rr"
"""The one axis that does not name a value: it names a rule that may be absent (his ask, 22/09).

Every other path substitutes a value into a container that is already there. This one has to
build or remove the container: a target is `{"type": "risk_multiple", "params": {"rr": 3}}`, and
"no target" is not an `rr` of anything — it is `take_profit: null`, which is what a setup that
conducts its own stop wants to be compared against.

⚠️ **The axis names the leaf, not the block, and that is the whole reason it is legible.** A grid
over `exit.take_profit` would carry those dictionaries as its values: the point's label would read
`take_profit={'type': 'risk_multiple', ...}`, the heatmap's axis would be objects, and the dataset
column with it. Named at the leaf, the values stay `3` and `None` — which every reader already
knows how to print, sort and plot, and `None` is the `off` the screen already writes."""


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
    if path == TAKE_PROFIT_RR:
        _set_take_profit(document, value)
        return
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


def _set_take_profit(document: dict[str, Any], value: Any) -> None:  # noqa: ANN401
    """Put a target of `value` times the risk on this document, or none at all for `None`.

    Written rather than substituted because the block is what changes: a document saved without a
    target has `take_profit: null`, so there is no `params` to reach into, and one saved with a
    target must lose the whole block rather than keep an `rr` of nothing.

    ⚠️ **The block is rebuilt, not patched.** `RiskMultipleParams` holds `rr` alone today; the day
    it holds a second field, patching would carry the base document's value for it into every
    point while the label claimed only `rr` varied. Rebuilding fails loudly instead — the new
    field would be missing and the DSL would refuse the point.
    """
    container, key = _walk(document, TAKE_PROFIT)
    if not isinstance(container, dict):  # pragma: no cover — `exit` is an object in every document
        raise GridError(f"{TAKE_PROFIT!r} does not name a place a value can be put")
    container[key] = None if value is None else {"type": "risk_multiple", "params": {"rr": value}}


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


def check_grid(document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]) -> None:
    """Raise `GridError` if this grid cannot be applied to this document — without expanding it.

    ⚠️ **For a caller that only needs the verdict.** Saving a catalogue entry used to call
    `expand` and throw the documents away; bounded by the old cap of 500 that was cheap, and with
    no cap it would build every combination of a grid of any size just to say it is well formed.
    The checks are the axes' alone — reachable, populated, distinct — so they cost the number of
    axes, not the size of the product.
    """
    _check_axes(document, grid)


def _check_axes(document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]) -> None:
    """Refuse a grid before expanding it — every axis reachable, populated, distinct.

    ⚠️ A **single-valued** axis is deliberately allowed, and this docstring used to claim
    otherwise. `{"period": [9], "rr": [2, 3]}` is a perfectly good study: it pins one parameter
    while searching another, which is how a reader isolates one axis of a grid they have already
    looked at. Refusing it would make a two-axis study impossible to narrow.
    """
    if not grid:
        raise GridError("a study needs at least one parameter to vary")

    for path, values in grid.items():
        # ⚠️ The check that matters most, and the only one whose absence is silent. A path this
        # document has nothing at — `setup.params.periodd` for a typo, or an axis meant for a
        # different strategy — would simply *add* a key. Nothing reads it: the setup factory
        # takes the parameters it knows and ignores the rest. The study would then run N
        # identical backtests, produce N identical results, and draw a perfectly flat heatmap
        # that looks like a finding about the market.
        # The target's axis is checked against the **block**, which every document has (as an
        # object or as `null`): its leaf is exactly what may not be there yet.
        _walk(dict(document), TAKE_PROFIT if path == TAKE_PROFIT_RR else path)
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


def expand(document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]) -> list[GridPoint]:
    """Every combination of the grid, as its own strategy document.

    Raises `GridError` — never a partial expansion — if any axis is unreachable, empty or
    repeats a value. Never for its size: every combination is expanded (see the note above
    `GridError`).

    The cartesian product is taken in the order the axes were declared, with the **last axis
    varying fastest**, which is `itertools.product`'s own order and the one a reader expects
    from a nested loop. It matters because that order becomes the order of the runs in the
    study, and a heatmap laid out row by row is reading it.
    """
    _check_axes(document, grid)

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
    if path == TAKE_PROFIT_RR:
        return _take_profit_of(document)
    container, key = _walk(dict(document), path)
    if isinstance(container, list):
        return container[int(key)]
    if isinstance(container, dict):
        return container[key]
    # `_walk` resolves the last segment through `_descend`, which refuses anything that is not a
    # list with that index or a dict with that key — so by here the container is always one of
    # the two above. Kept loud rather than deleted for the same reason `_set`'s twin is: without
    # it the function would fall off the end and return `None`, and a point whose value could
    # not be read would place on the grid as an axis nobody set.
    raise GridError(  # pragma: no cover — unreachable while `_walk` resolves the last segment
        f"{path!r} does not name a place a value can be read from"
    )


def _take_profit_of(document: Mapping[str, Any]) -> Any:  # noqa: ANN401
    """The R multiple this document targets, or `None` when it has no target at all.

    The inverse of `_set_take_profit`, and it is what puts a run back on the axis it belongs to:
    a heatmap reading `exit.take_profit.params.rr` off a document saved without a target must get
    `None` — the value the grid was given — rather than a refusal about a path into `null`.
    """
    container, key = _walk(dict(document), TAKE_PROFIT)
    target = container[key] if isinstance(container, dict) else None
    if target is None:
        return None
    if not isinstance(target, dict):
        raise GridError(f"{TAKE_PROFIT!r} holds {target!r}, which is not a target")
    params = target.get("params")
    if not isinstance(params, dict) or "rr" not in params:
        raise GridError(f"{TAKE_PROFIT!r} holds a target with no {'rr'!r}")
    return params["rr"]


def read_point(document: Mapping[str, Any], grid: Mapping[str, Sequence[Any]]) -> dict[str, Any]:
    """Which point of `grid` a stored document is: the value it holds at each axis.

    The inverse of expanding, and it exists so a reader can place a run on the grid without
    parsing the caption it was given. Two callers need it — the study read and the walk-forward
    read — and they need it to agree, since one places heatmap cells and the other names the
    point a fold chose.

    ⚠️ **An unreachable path yields `None` rather than raising.** The grid comes from a stored
    row somebody could edit, and a study whose grid names a parameter its documents lack is a
    study that can still be read: every other axis places and the odd one out sorts last. A
    reporting endpoint that 500s on a surprising value is worse than one that shows what it has.
    """
    return {path: _read_or_none(document, path) for path in grid}


def _read_or_none(document: Mapping[str, Any], path: str) -> Any:  # noqa: ANN401
    try:
        return value_at(document, path)
    except GridError:
        return None


def coordinates(grid: Mapping[str, Sequence[Any]], values: Mapping[str, Any]) -> tuple[int, ...]:
    """Where a point sits on the grid: one index per axis, in the order the axes were declared.

    Sorting by this reproduces the order `expand` produced — last axis varying fastest — from
    nothing but the stored grid and the documents, which is why the ordering survives a
    re-collection, a re-read, or rows arriving in any order at all. It is also the tie-break a
    walk-forward's `choose` uses, and the two must be the same rule: a tie broken one way here
    and another way there would make "the best point" depend on which endpoint was asked.

    An axis value the grid no longer lists sorts last rather than raising, for the reason
    `read_point` gives.
    """
    return tuple(
        list(axis).index(values[path]) if values[path] in list(axis) else len(axis)
        for path, axis in grid.items()
    )


def named(base_name: str, point: GridPoint) -> str:
    """The stored name for a point's strategy: the base name plus what makes it different.

    `MME9 breakout [period=9, take_profit_rr=3]`. The bracketed part is the whole reason this
    exists rather than letting the points be versions 1..N of one lineage: they *are* stored as
    separate rows either way, and a run log row reading `MME9 breakout v37` cannot be read at
    all — the run log is exactly where a study's result gets read.
    """
    return fit_name(base_name, point.label, point.values)


def _digest(values: Mapping[str, Any]) -> str:
    """Six hex characters that stand for this point's coordinates.

    ⚠️ **sha256, not `hash()`.** The built-in is salted per process, so a name built today and the
    same name rebuilt tomorrow would differ — and a stored strategy would be written twice instead
    of being found and reused.
    """
    canonical = json.dumps(dict(sorted(values.items())), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:6]


def fit_name(base_name: str, label: str, values: Mapping[str, Any]) -> str:
    """`{base} [{label}]`, kept inside the DSL's own limit on a document's name.

    ⚠️ **Why this exists, measured on a real sweep.** A five-axis entry on this project's own
    shelf expanded to 120 documents and every one was refused, for `name: String should have at
    most 120 characters` — the length of a caption, which says nothing about the strategy behind
    it. That entry contributed zero runs to a sweep that looked like it had launched. (The two
    120s are a coincidence: one is the number of points, the other the limit.)

    Trimmed rather than the limit raised: the limit is the DSL's contract, and a 300-character
    name is one nobody reads — the run log prints it in full, beside the version and the dates.

    ⚠️ **The suffix is what makes trimming safe.** Neighbouring points of a grid differ only in
    the axis that varies fastest, so a trim can leave them sharing every visible character;
    without the digest the second one would collide on the unique `(name, version)` and come back
    as an integrity error from the database — trading a refusal anyone can read for one nobody
    can.
    """
    full = f"{base_name} [{label}]"
    if len(full) <= NAME_MAX_LENGTH:
        return full

    tail = f"… #{_digest(values)}]"
    room = NAME_MAX_LENGTH - len(base_name) - len(" [") - len(tail)
    if room < 0:
        # The base name alone does not leave room for the digest. Trim it too: the digest is what
        # keeps two points apart, so it is the part that must survive.
        #
        # ⚠️ A mutant swapping `< 0` for `< 1` survives the suite, and it is **under-specified**
        # rather than equivalent: at exactly zero room the two differ by one character — `< 1`
        # trims the base by one so a single character of label survives, `< 0` keeps the base and
        # shows none. Both land on the limit and both carry the digest, so nothing a caller
        # depends on changes; pinning it would take a golden on a 108-character base name, and
        # that golden would be a test of caption cosmetics.
        base_name = base_name[: max(1, NAME_MAX_LENGTH - len(" [") - len(tail) - 1)]
        room = NAME_MAX_LENGTH - len(base_name) - len(" [") - len(tail)
    return f"{base_name} [{label[: max(0, room)].rstrip(', ')}{tail}"
