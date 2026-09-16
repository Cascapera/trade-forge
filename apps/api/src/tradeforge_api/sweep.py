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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tradeforge_api.grid import GridError, GridPoint, expand, fit_name, size_of

SECONDS_PER_BACKTEST = 0.23
"""What a backtest costs, measured rather than guessed.

The mean over this project's own 1 181 finished runs; the slowest was 2.17 s. Named because the
cap below is derived from it — a budget stated as a number of runs is a budget that stops being
true the day the engine gets faster or the histories get longer.
"""

MAX_SWEEP_RUNS = 3000
"""How many backtests one sweep will enqueue.

A cap and not a sample: a sweep too large is refused with its own size in the message, never
quietly trimmed. Half a sweep is a picture of a space that was never searched, and it looks
exactly like a picture of one that was.

⚠️ **The number is a time budget, and it was raised once already.** The first draft said 2 000,
which refused three catalogue entries of fifty points each over five markets and three
timeframes — 2 250 runs, and the exact shape this feature was asked for. A cap that refuses the
feature is not a cap, and the test that caught it asserts the *budget* rather than that one
shape: at the measured mean this is under twelve minutes on one worker, and even every run being
the slowest ever seen keeps it inside two hours.

Higher than `MAX_POINTS` because a sweep multiplies three ways where a study multiplies one.
Low enough that launching one is still a deliberate act.
"""


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


def _whole(definition: Mapping[str, Any]) -> GridPoint:
    """The base document as a single point, for an entry that varies nothing.

    Spelled out rather than handed to `expand` with an empty grid, because that one refuses an
    empty axis and an empty grid is not an empty axis — it is the absence of a question, and the
    answer to it is the document itself.
    """
    # `label` is a property over `values`, so an empty `values` gives an empty label — the two
    # cannot disagree, which is why it is a property rather than a field.
    return GridPoint(values={}, document=dict(definition))


def points_in(grid: Mapping[str, Sequence[Any]]) -> int:
    """How many points one entry's grid holds — 1 for an entry that varies nothing."""
    return size_of(grid)


def size_refusal(runs: int) -> str | None:
    """Why a sweep of this many runs cannot be launched, in the words both endpoints say.

    ⚠️ **One sentence with one owner, because two endpoints ask this question.** The preview
    reports it as an `error` and the launch raises it as a 422 — different mechanisms, and that
    part is right: a preview that raised would have nothing to preview. What must not differ is
    the *answer*. Written out twice, the two copies drift the first time either is reworded, and
    a person then reads one verdict on the screen and gets another on the button.

    ⚠️ **Zero is a refusal, not a small number.** A sweep with nothing runnable is a question
    that cannot be asked, and `0 runs` rendered beside a live launch button invites pressing it.

    The count handed in is the **net** one — what would actually be enqueued, refusals already
    subtracted. The cap is a time budget, and combinations that never run spend no time.
    """
    if runs > MAX_SWEEP_RUNS:
        return (
            f"this sweep expands to {runs} backtests, over the {MAX_SWEEP_RUNS} one sweep will run"
        )
    if runs == 0:
        return "no combination in this sweep can run"
    return None


__all__ = [
    "MAX_SWEEP_RUNS",
    "SECONDS_PER_BACKTEST",
    "SweepDocument",
    "SweepError",
    "documents_for",
    "points_in",
    "size_refusal",
]
