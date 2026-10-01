"""His bar for ranking a sweep's runs (2026-10-01): apart from the bar for keeping them, and higher
on H4 and above, where the keeping floor is zero."""

from tradeforge_api.ranking_floor import RANK_MIN_TRADES
from tradeforge_api.retention import MIN_TRADES
from tradeforge_schema.models import TIMEFRAMES


def test_every_chart_the_dsl_names_has_a_ranking_floor() -> None:
    """A chart added to the DSL without a line here would rank on no floor at all — this says so
    first, in a test."""
    assert set(RANK_MIN_TRADES) == set(TIMEFRAMES)


def test_the_ranking_floors_are_his() -> None:
    assert RANK_MIN_TRADES == {
        "M1": 60,
        "M5": 60,
        "M15": 30,
        "M30": 30,
        "H1": 30,
        "H4": 20,
        "D1": 10,
        "W1": 5,
    }


def test_ranking_never_asks_less_than_keeping() -> None:
    """A run good enough to rank is always one whose trades were kept, if it made money."""
    assert all(RANK_MIN_TRADES[chart] >= MIN_TRADES[chart] for chart in TIMEFRAMES)
