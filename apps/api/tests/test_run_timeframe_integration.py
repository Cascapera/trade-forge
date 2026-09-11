"""The run's timeframe refused at every door that opens a run.

`test_run_timeframe.py` proves the rule on documents, with no database. What only exists here is
that the rule is actually **wired**: three endpoints create runs, and a guard written but called
from none of them leaves that file green and the hole open.

⚠️ Each endpoint is asserted with the filtered strategy **and** the filterless one, at the same
timeframe. Without the second half these tests pass on a router that refuses every timeframe
disagreement — which would break `Re-run saved` for every strategy that has no higher-timeframe
filter, and that is most of them.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.models import Instrument
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)
CAPITAL = "10000"


class _CapturingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...]]] = []

    async def enqueue_job(self, function: str, *args: Any, **options: Any) -> None:
        self.jobs.append((function, args))


def _seed(session: Session) -> None:
    for symbol in ("EURUSD", "GBPUSD"):
        session.add(
            Instrument(
                symbol=symbol,
                name=f"{symbol} for the timeframe tests",
                asset_class=AssetClass.FOREX,
                currency_base=symbol[:3],
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=Decimal("8"),
            )
        )
    session.commit()


def _filtered(name: str) -> dict[str, Any]:
    """Saved at M15 under an H4 filter — legal, and the shipped fixture's shape.

    ⚠️ Run at H4 the filter is no longer coarser than the chart, and the engine says nothing:
    it assembles one "H4" bar per H4 bar, a bar late. That silence is what these tests close.
    """
    return {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "M15",
        "setup": {
            "type": "structure_choch",
            "params": {"htf": "H4", "htf_offset": 3, "stop_buffer": 0.1},
        },
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 3}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 0.5}}},
    }


def _filterless(name: str) -> dict[str, Any]:
    document = _filtered(name)
    document["setup"] = {"type": "structure_choch", "params": {"stop_buffer": 0.1}}
    return document


def _app(
    settings: Settings, session_factory: Callable[[], Session], tmp_path: Path, queue: Any
) -> Any:
    return create_app(
        settings=settings.model_copy(update={"parquet_root": tmp_path}),
        session_factory=session_factory,
        arq_pool=queue,
    )


def _window() -> dict[str, Any]:
    return {
        "date_from": START.isoformat(),
        "date_to": (START + 100 * HOUR).isoformat(),
        "initial_capital": CAPITAL,
    }


@pytest.fixture
def client(session_factory: Callable[[], Session], settings: Settings, tmp_path: Path) -> Any:
    seeding = session_factory()
    _seed(seeding)
    seeding.close()
    with TestClient(_app(settings, session_factory, tmp_path, _CapturingQueue())) as opened:
        yield opened


def _save(client: Any, document: dict[str, Any]) -> str:
    created = client.post("/strategies", json=document)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def test_a_backtest_at_the_filters_own_timeframe_is_refused(client: Any) -> None:
    filtered = _save(client, _filtered(f"choch {uuid.uuid4()}"))
    filterless = _save(client, _filterless(f"plain {uuid.uuid4()}"))

    refused = client.post(
        "/backtests",
        json={"strategy_id": filtered, "symbol": "EURUSD", "timeframe": "H4", **_window()},
    )
    assert refused.status_code == 422
    # The DSL's own sentence, not one written in the router — so a screen that already renders a
    # semantic refusal renders this unchanged.
    assert "htf" in refused.text

    # ⚠️ Same timeframe, same window, no filter: accepted. This is the line that fails on a
    # router comparing two strings instead of asking whether the document can run.
    allowed = client.post(
        "/backtests",
        json={"strategy_id": filterless, "symbol": "EURUSD", "timeframe": "H4", **_window()},
    )
    assert allowed.status_code == 202, allowed.text


def test_a_backtest_at_a_timeframe_the_filter_still_covers_is_accepted(client: Any) -> None:
    # H1 under an H4 filter: coarser and a whole number of bars, so the filter still filters.
    # Without this case the guard could refuse every disagreement and look correct.
    filtered = _save(client, _filtered(f"choch {uuid.uuid4()}"))

    accepted = client.post(
        "/backtests",
        json={"strategy_id": filtered, "symbol": "EURUSD", "timeframe": "H1", **_window()},
    )
    assert accepted.status_code == 202, accepted.text


def test_a_basket_is_refused_whole_rather_than_per_market(client: Any) -> None:
    filtered = _save(client, _filtered(f"choch {uuid.uuid4()}"))

    refused = client.post(
        "/baskets",
        json={
            "strategy_id": filtered,
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframe": "H4",
            **_window(),
        },
    )
    # One timeframe for every market, so the answer cannot differ between them — and nothing is
    # written, like every other basket refusal.
    assert refused.status_code == 422
    assert "htf" in refused.text


def test_a_study_is_refused_before_a_single_point_is_written(client: Any) -> None:
    filtered = _save(client, _filtered(f"choch {uuid.uuid4()}"))
    body = {
        "strategy_id": filtered,
        "symbol": "EURUSD",
        "timeframe": "H4",
        "cost_model": {"type": "none"},
        "grid": {"setup.params.stop_buffer": [0.1, 0.2]},
        **_window(),
    }

    refused = client.post("/studies", json=body)
    assert refused.status_code == 422

    # ⚠️ Nothing queued and nothing stored: a study that half-exists answers a question nobody
    # asked. The strategy list is the observable — a launch that got halfway would have written
    # its point strategies before failing.
    listed = client.get("/strategies", params={"include_generated": True, "limit": 200})
    assert all("stop_buffer" not in item["name"] for item in listed.json()["items"])

    # And the same grid at the document's own timeframe goes through.
    assert client.post("/studies", json={**body, "timeframe": "M15"}).status_code == 202


def test_the_preview_answers_at_the_timeframe_the_study_would_use(client: Any) -> None:
    filtered = _save(client, _filtered(f"choch {uuid.uuid4()}"))
    grid = {"setup.params.stop_buffer": [0.1, 0.2]}

    at_m15 = client.post(
        "/studies/preview", json={"strategy_id": filtered, "grid": grid, "timeframe": "M15"}
    )
    assert at_m15.status_code == 200
    assert at_m15.json()["refusals"] == []

    at_h4 = client.post(
        "/studies/preview", json={"strategy_id": filtered, "grid": grid, "timeframe": "H4"}
    )
    assert at_h4.status_code == 200
    # ⚠️ The agreement that makes a preview worth having: the launch above refuses this exact
    # request, so the preview has to say so too.
    assert len(at_h4.json()["refusals"]) == 2

    # The field is required rather than optional, because a preview that skipped the check when
    # it was absent would agree with the launch except where it matters.
    without = client.post("/studies/preview", json={"strategy_id": filtered, "grid": grid})
    assert without.status_code == 422
