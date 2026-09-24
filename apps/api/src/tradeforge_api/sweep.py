"""Expanding a sweep into the documents and runs it becomes.

A study varies the parameters and holds the market still. A basket varies the market and holds
the parameters still. A sweep takes the product of both and adds a third axis, the timeframe —
which is what "run every variation I care about, over everything I have collected" means.

Pure on purpose, like `grid`: no database, no HTTP, no engine. Everything worth being wrong
about here is arithmetic over dictionaries, and arithmetic that needs a Postgres container to be
tested is arithmetic nobody tests at the edges.

⚠️ **The timeframe is written into the document, not only onto the run.** Since PR-238 a
document and its run must agree under a higher-timeframe filter, because the filter's bars are
assembled from the document's own width — a sweep that varied only the run's would produce
exactly the disagreement that rule exists to refuse. Writing it in also makes the documents
distinct per timeframe, which is what lets them keep being deduplicated by content.

⚠️ **What this module makes easy is the mistake it makes easy.** Fifty points over five markets
and three timeframes is seven hundred and fifty measurements, and the best of them is the best
of seven hundred and fifty draws. The engine is deterministic by invariant — measured on this
project's own data as three runs identical trade for trade — so re-running a winner over the
same window returns the identical number and is not a second opinion. Only data the winner was
**not chosen on** is: a walk-forward, another market, a reserved window.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tradeforge_api.grid import (
    TAKE_PROFIT_RR,
    GridError,
    GridPoint,
    expand,
    fit_name,
    size_of,
)


class SweepError(ValueError):
    """A sweep that cannot be expanded into runs anyone should launch."""


@dataclass(frozen=True, slots=True)
class SweepDocument:
    """One strategy document a sweep will write: an entry, at a timeframe, at a grid point."""

    entry_id: str
    """Which catalogue entry it came from — the label the reader chose, by id."""

    timeframe: str
    """The chart, and it is **also** inside `document`. Carried out here because the run needs
    it as a column and reading it back out of the document would be parsing what we just wrote."""

    label: str
    """`M15 · period=9` — what makes this document different from its siblings. Read on a run
    log row, which is where a sweep's result actually gets looked at."""

    values: Mapping[str, Any]
    """The point's coordinates, keyed by the grid's own dotted paths, with the timeframe under
    `timeframe`. ⚠️ Place a heatmap cell from these, never by splitting `label`: a label is a
    caption and splitting one works until a value contains a separator."""

    document: Mapping[str, Any]
    """The document itself, with the point's values substituted and the timeframe written in."""


def documents_for(
    *,
    entry_id: str,
    entry_name: str,
    definition: Mapping[str, Any],
    grid: Mapping[str, Sequence[Any]],
    timeframes: Sequence[str],
) -> list[SweepDocument]:
    """Every document one catalogue entry becomes across the timeframes asked for.

    The grid is expanded once and reused at each timeframe rather than expanded per timeframe:
    the axes are the same and `expand` is the component that owns what a grid means, so asking
    it repeatedly would be asking one question several times and hoping for one answer.

    ⚠️ **An entry with no grid is one document per timeframe, not zero.** `expand` over an empty
    grid returns the base document unchanged, which is the empty product spelled as an object —
    the same 1 that `size_of({})` gives.
    """
    if not timeframes:
        raise SweepError("a sweep needs at least one timeframe")

    # ⚠️ **No target unless the grid names one** (his call, 23/09). The target is measured, not
    # guessed: a run without one records how far each trade went (MFE), and every target on the
    # ladder is scored from that afterwards (`excursion.target_ladder`) — one run instead of one
    # per target. Left alone, every point would inherit whatever target the saved document had,
    # the author's 5 R on his template, and the ladder above that rung could not be read.
    if TAKE_PROFIT_RR not in grid:
        definition = _without_target(definition)

    try:
        points = expand(definition, grid) if grid else [_whole(definition)]
    except GridError as exc:
        raise SweepError(f"{entry_name}: {exc}") from exc

    out: list[SweepDocument] = []
    for timeframe in timeframes:
        for point in points:
            # ⚠️ The timeframe goes **into** the document, and the name carries it. Without the
            # name the two timeframes produce documents that differ in content but share a name,
            # and (name, version) is unique — the second one would collide rather than insert.
            document = {**point.document, "timeframe": timeframe}
            label = timeframe if point.label == "" else f"{timeframe} · {point.label}"
            values = {"timeframe": timeframe, **point.values}
            # ⚠️ `fit_name`, not an f-string: a grid with five axes writes a label longer than the
            # DSL allows a name to be, and every point of it was refused for that — 120 documents,
            # zero runs. The full label is kept on the point either way, which is what the sweep's
            # screen and its dataset read.
            document["name"] = fit_name(entry_name, label, values)
            out.append(
                SweepDocument(
                    entry_id=entry_id,
                    timeframe=timeframe,
                    label=label,
                    values=values,
                    document=document,
                )
            )
    return out


def _without_target(definition: Mapping[str, Any]) -> dict[str, Any]:
    """The document with its target removed, and nothing else touched. A document with no `exit`
    block has no target to remove, and is returned as it came."""
    exit_block = definition.get("exit")
    if not isinstance(exit_block, Mapping) or exit_block.get("take_profit") is None:
        return dict(definition)
    return {**definition, "exit": {**exit_block, "take_profit": None}}


def _whole(definition: Mapping[str, Any]) -> GridPoint:
    """The base document as a single point, for an entry that varies nothing.

    Spelled out rather than handed to `expand` with an empty grid, because that one refuses an
    empty axis and an empty grid is not an empty axis — it is the absence of a question, and the
    answer to it is the document itself.
    """
    # `label` is a property over `values`, so an empty `values` gives an empty label — the two
    # cannot disagree, which is why it is a property rather than a field.
    return GridPoint(values={}, document=dict(definition))


UnreadParams = Callable[[Mapping[str, Any]], frozenset[str]]
"""Given a document's `setup` block, the parameters its setup never reads — the engine's
`unread_params`, handed in so that this module stays free of the engine."""


def shared(documents: Sequence[SweepDocument], unread: UnreadParams) -> dict[int, SweepDocument]:
    """The documents another one answers, keyed by `id()`: each to the first that runs the same.

    Two points of one entry at one timeframe run trade for trade the same when their documents
    differ only in a parameter the setup never reads — `stop_buffer` under the `martelo` entry is
    four documents and one behaviour (24/09). His answer was to run it once and let the others
    point at it: every point stays in the sweep, and the dataset keeps a row for each.

    ⚠️ **The key is the document, less its name and less what the setup does not read — and the
    entry.** The name carries the point's label, so it differs by construction and decides
    nothing. The entry is kept in the key although two entries can hold the same document: a
    point answered by another entry's run would lose the entry it belongs to on every screen that
    groups by entry. Anything left in the key can only split a group that could have been one,
    which costs a run; anything wrongly left out would merge two behaviours, which costs the
    answer — so the engine's table is the only thing trusted to leave something out.

    The first document of a group, in the order given, answers for the rest: launch order, so
    which point owns the run does not change between a preview and the launch.
    """
    owners: dict[str, SweepDocument] = {}
    answered: dict[int, SweepDocument] = {}
    for doc in documents:
        owner = owners.setdefault(_behaviour(doc, unread), doc)
        if owner is not doc:
            answered[id(doc)] = owner
    return answered


def _behaviour(doc: SweepDocument, unread: UnreadParams) -> str:
    """What decides how `doc` runs, as text two equal behaviours spell the same way."""
    body = {key: value for key, value in doc.document.items() if key != "name"}
    setup = body.get("setup")
    if isinstance(setup, Mapping) and isinstance(params := setup.get("params"), Mapping):
        ignored = unread(setup)
        if ignored:
            kept = {key: value for key, value in params.items() if key not in ignored}
            body["setup"] = {**setup, "params": kept}
    return json.dumps([doc.entry_id, body], sort_keys=True)


def points_in(grid: Mapping[str, Sequence[Any]]) -> int:
    """How many points one entry's grid holds — 1 for an entry that varies nothing."""
    return size_of(grid)


def size_refusal(runs: int) -> str | None:
    """Why a sweep of this many runs cannot be launched, in the words both endpoints say.

    ⚠️ **Only an empty sweep, never a large one** (his decision, 18/09: "não me importa que demore
    horas rodando"). The cap that was here — 3000 runs, sold as twelve minutes at 0.23 s a run —
    refused 3168 runs of his own sweep, on a day a run was measured at about 43 s. How long a
    sweep takes is something to show, not a ground to refuse.

    ⚠️ **One sentence with one owner, because two endpoints ask this question.** The preview
    reports it as an `error` and the launch raises it as a 422 — different mechanisms, and that
    part is right: a preview that raised would have nothing to preview. What must not differ is
    the *answer*. Written out twice, the two copies drift the first time either is reworded, and
    a person then reads one verdict on the screen and gets another on the button.

    ⚠️ **Zero is a refusal, not a small number.** A sweep with nothing runnable is a question
    that cannot be asked, and `0 runs` rendered beside a live launch button invites pressing it.

    The count handed in is the **net** one — what would actually be enqueued, refusals already
    subtracted.
    """
    if runs == 0:
        return "no combination in this sweep can run"
    return None


__all__ = [
    "SweepDocument",
    "SweepError",
    "UnreadParams",
    "documents_for",
    "points_in",
    "shared",
    "size_refusal",
]
