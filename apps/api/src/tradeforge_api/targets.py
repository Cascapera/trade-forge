"""The target ladder across a sweep entry's runs — one row per rung (2026-09-23).

Every run of a sweep scores the whole ladder while its trades are in memory
(`backtest_metrics.targets`). Here the rungs of an entry's runs are put side by side: how many runs
each rung could score, how many it made positive, the median expectancy per trade, and the best
run at it. Per entry, never pooled, for the reason a sweep summarises nothing across entries.

⚠️ **Medians, and the best only beside them.** A sweep searches many points; the best run at any
rung is the best of many draws, and read alone it is the flattering answer to a question nobody
asked. The median is what says whether the rung helps the method or only its luckiest point.
"""

from collections.abc import Sequence
from decimal import Decimal
from statistics import median

from tradeforge_api.schemas import TargetRungOut
from tradeforge_db.models import Backtest
from tradeforge_engine.excursion import LADDER


def rungs_across(runs: Sequence[tuple[Backtest, str]]) -> list[TargetRungOut]:
    """One row per rung of the ladder, lowest target first, over the runs that scored it."""
    out: list[TargetRungOut] = []
    for rung in LADDER:
        key = format(rung.normalize(), "f")
        scored: list[tuple[Decimal, Decimal, str]] = []
        for run, label in runs:
            ladder = None if run.metrics is None else run.metrics.targets
            outcome = None if ladder is None else ladder.get(key)
            # A run with no trades scores every rung at zero, and counting it would pull each
            # median towards zero for a run that said nothing about any target.
            if outcome is None or outcome["trades"] == 0:
                continue
            scored.append((Decimal(outcome["net_r"]), Decimal(outcome["expectancy_r"]), label))
        best = max(scored, key=lambda one: one[0], default=None)
        out.append(
            TargetRungOut(
                rung=key,
                runs_scored=len(scored),
                runs_positive=sum(1 for net, _, _ in scored if net > 0),
                median_expectancy_r=median(e for _, e, _ in scored) if scored else None,
                best_label=None if best is None else best[2],
                best_net_r=None if best is None else best[0],
            )
        )
    return out


__all__ = ["rungs_across"]
