"""The picture a signal is posted with (signals PR 7): candles, the setup's own drawing, the levels.

Drawn with matplotlib on the server, in the screen's colours (`apps/web/src/backtest/price.ts`):
close to the trade snapshot a run shows, not a pixel copy of it — his choice of 08/10, against
photographing the web page in a headless browser.

The setup's drawing is the `EntrySnapshot` the strategy attached to its order — the same regions,
levels and series the result screen draws — so the picture shows *why* it armed, not only where.

⚠️ The x axis is the **bar's position**, not its time: a night or a weekend would otherwise open a
gap wider than the setup, and a B3 chart would be mostly empty.
"""

import bisect
import datetime as dt
import io
from collections.abc import Sequence
from decimal import Decimal

import matplotlib as mpl

mpl.use("Agg")  # no display: this runs in a session process, never in front of anyone

import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from tradeforge_engine.domain import Candle, EntrySnapshot

__all__ = ["render_signal_png"]

_BACKGROUND = "#070d1f"
_GRID = "#1e293b"
_TEXT = "#cbd5e1"
_UP = "#1FA97E"
_DOWN = "#D96047"
_ENTRY = "#5F8AD2"
_STOP = "#D96047"
_TARGET = "#1FA97E"
_CURVES = ("#BC8620", "#d55181", "#3987e5")
_REGION = "#94a3b8"
_TICKS = 6
_BRASILIA = dt.timezone(dt.timedelta(hours=-3))


def _f(value: Decimal) -> float:
    return float(value)


def render_signal_png(  # noqa: PLR0913 — what a picture of a signal shows, each its own line
    candles: Sequence[Candle],
    *,
    title: str,
    entry: Decimal | None,
    stop: Decimal | None,
    target: Decimal | None,
    snapshot: EntrySnapshot | None = None,
    exit_price: Decimal | None = None,
) -> bytes:
    """A PNG of the candles with the setup's drawing and the signal's levels."""
    bars = list(candles)
    if not bars:
        raise ValueError("a signal picture needs at least one candle")
    instants = [bar.time for bar in bars]

    def at(moment: dt.datetime) -> float:
        """The position of the bar holding `moment` — before the first is the left edge."""
        return float(max(bisect.bisect_right(instants, moment) - 1, 0))

    last = float(len(bars) - 1)
    figure, axes = plt.subplots(figsize=(10, 5.6), dpi=110)
    figure.patch.set_facecolor(_BACKGROUND)
    axes.set_facecolor(_BACKGROUND)

    if snapshot is not None:
        for region in snapshot.regions:
            start = at(region.from_time)
            axes.add_patch(
                Rectangle(
                    (start - 0.5, _f(region.bottom)),
                    last - start + 1.5,  # a region stands until the setup lets it go: to the edge
                    _f(region.top) - _f(region.bottom),
                    facecolor=_REGION,
                    alpha=0.12,
                    edgecolor=_REGION,
                    linewidth=0.6,
                )
            )
        for index, series in enumerate(snapshot.series):
            if series.points:
                axes.plot(
                    [at(point.time) for point in series.points],
                    [_f(point.value) for point in series.points],
                    color=_CURVES[index % len(_CURVES)],
                    linewidth=1.1,
                    label=series.label,
                )
        for level in snapshot.levels:
            axes.hlines(
                _f(level.price),
                at(level.from_time),
                at(level.to_time),
                colors=_REGION,
                linestyles="dotted",
                linewidth=0.9,
            )

    for x, bar in enumerate(bars):
        colour = _UP if bar.close >= bar.open else _DOWN
        axes.vlines(x, _f(bar.low), _f(bar.high), colors=colour, linewidth=0.8)
        low, high = sorted((_f(bar.open), _f(bar.close)))
        axes.add_patch(Rectangle((x - 0.35, low), 0.7, max(high - low, 1e-12), color=colour))

    for price, colour, label, style in (
        (entry, _ENTRY, "Entrada", "solid"),
        (stop, _STOP, "Stop", "dashed"),
        (target, _TARGET, "Alvo", "dashed"),
        (exit_price, _TEXT, "Saída", "dotted"),
    ):
        if price is not None:
            axes.axhline(_f(price), color=colour, linestyle=style, linewidth=1.2)
            axes.annotate(
                f"{label} {price.normalize():f}",
                xy=(1.0, _f(price)),
                xycoords=("axes fraction", "data"),
                xytext=(4, 0),
                textcoords="offset points",
                va="center",
                color=colour,
                fontsize=8,
            )

    ticks = sorted({round(last * i / (_TICKS - 1)) for i in range(_TICKS)})
    axes.set_xticks(ticks)
    axes.set_xticklabels(
        [bars[i].time.astimezone(_BRASILIA).strftime("%d/%m %H:%M") for i in ticks]
    )
    axes.set_xlim(-1, last + 4)
    axes.tick_params(colors=_TEXT, labelsize=8)
    for spine in axes.spines.values():
        spine.set_color(_GRID)
    axes.grid(color=_GRID, linewidth=0.5)
    axes.set_title(title, color=_TEXT, fontsize=11, loc="left")
    if snapshot is not None and snapshot.series:
        axes.legend(loc="upper left", fontsize=7, facecolor=_BACKGROUND, labelcolor=_TEXT)
    figure.tight_layout()

    out = io.BytesIO()
    figure.savefig(out, format="png", facecolor=_BACKGROUND)
    plt.close(figure)
    return out.getvalue()
