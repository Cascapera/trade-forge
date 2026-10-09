"""`/live-setups` over real Postgres: setups, their markets, their signals' history (09/10)."""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.brokers import broker_for_server
from tradeforge_db.live_setups import followed_pairs, record_event
from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def client(session_factory: Callable[[], Session]) -> Iterator[TestClient]:
    app: Any = create_app(settings=Settings(), session_factory=session_factory)
    with TestClient(app) as opened:
        yield opened


def instrument(session: Session, symbol: str) -> uuid.UUID:
    found = session.query(Instrument).filter_by(symbol=symbol).one_or_none()
    if found is not None:
        return found.id
    made = Instrument(
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.FUTURE,
        currency_quote="BRL",
        tick_size=Decimal(1),
        tick_value=Decimal("0.2"),
        contract_size=Decimal(1),
        digits=0,
        broker_id=broker_for_server(session, "XPMT5-DEMO").id,
        broker_symbol=f"{symbol}$",
    )
    session.add(made)
    session.flush()
    return made.id


def a_run(
    session_factory: Callable[[], Session],
    *,
    symbol: str = "WIN",
    strategy_id: uuid.UUID | None = None,
    status: BacktestStatus = BacktestStatus.DONE,
) -> tuple[uuid.UUID, uuid.UUID]:
    """A finished H1 run; returns (run, strategy)."""
    with session_factory() as session:
        if strategy_id is None:
            strategy = Strategy(
                definition={"schema_version": "1.0", "name": "CHOCH BASE", "entry": {}, "exit": {}},
                version=1,
            )
            session.add(strategy)
            session.flush()
            strategy_id = strategy.id
        run = Backtest(
            strategy_id=strategy_id,
            instrument_id=instrument(session, symbol),
            timeframe="H1",
            date_from=START,
            date_to=START + dt.timedelta(days=30),
            initial_capital=Decimal(10000),
            cost_model={"type": "spread", "spread_points": "5"},
            status=status,
            engine_version="0.6.0",
        )
        session.add(run)
        session.commit()
        return run.id, strategy_id


def test_watching_a_run_makes_a_setup_on_its_market(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, _ = a_run(session_factory)

    response = client.post("/live-setups/from-run", json={"backtest_id": str(run)})

    assert response.status_code == 201, response.text
    setup = response.json()
    assert (setup["timeframe"], setup["active"], setup["no_target_r"]) == ("H1", True, "5.00000000")
    assert [(m["symbol"], m["broker"], m["active"]) for m in setup["markets"]] == [
        ("WIN", "xp", True)
    ]
    assert setup["metrics"]["signals"] == 0


def test_watching_another_run_of_the_same_setup_adds_its_market(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """His ask: a setup chosen from the runs gains other markets — not a second setup."""
    first, strategy = a_run(session_factory)
    second, _ = a_run(session_factory, symbol="WDO", strategy_id=strategy)

    client.post("/live-setups/from-run", json={"backtest_id": str(first)})
    client.post("/live-setups/from-run", json={"backtest_id": str(second)})

    [setup] = client.get("/live-setups").json()
    assert [m["symbol"] for m in setup["markets"]] == ["WIN", "WDO"]


def test_markets_are_added_switched_and_dropped_at_will(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, _ = a_run(session_factory)
    setup = client.post("/live-setups/from-run", json={"backtest_id": str(run)}).json()
    with session_factory() as session:
        bit = instrument(session, "BIT")
        session.commit()
    base = f"/live-setups/{setup['id']}/markets"

    added = client.post(base, json={"instrument_id": str(bit)}).json()
    win = added["markets"][0]["instrument_id"]
    switched = client.patch(f"{base}/{win}", json={"active": False}).json()
    dropped = client.delete(f"{base}/{bit}").json()

    assert [m["symbol"] for m in added["markets"]] == ["WIN", "BIT"]
    assert [m["active"] for m in switched["markets"]] == [False, True]
    assert [m["symbol"] for m in dropped["markets"]] == ["WIN"]
    with session_factory() as session:
        assert followed_pairs(session) == [], "the only market left is off"


def test_the_same_strategy_on_the_same_market_is_followed_once(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, strategy = a_run(session_factory)
    with session_factory() as session:
        win = instrument(session, "WIN")
        session.commit()
    client.post("/live-setups/from-run", json={"backtest_id": str(run)})

    twice = client.post(
        "/live-setups",
        json={"strategy_id": str(strategy), "timeframe": "H1", "instrument_ids": [str(win)]},
    )

    assert twice.status_code == 409


def test_only_a_finished_run_can_be_followed(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    running, _ = a_run(session_factory, status=BacktestStatus.RUNNING)

    response = client.post("/live-setups/from-run", json={"backtest_id": str(running)})

    assert response.status_code == 404


def event(setup: dict[str, Any], number: int, kind: str, **fields: str) -> dict[str, str]:
    return {
        "setup_id": setup["id"],
        "strategy_id": setup["strategy_id"],
        "instrument_id": setup["markets"][0]["instrument_id"],
        "number": str(number),
        "kind": kind,
        "symbol": "WIN",
        "timeframe": "H1",
        "side": "long",
        "time": "2026-10-09T14:00:00+00:00",
        **fields,
    }


def test_the_history_and_its_metrics_come_from_the_signals(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, _ = a_run(session_factory)
    setup = client.post("/live-setups/from-run", json={"backtest_id": str(run)}).json()
    with session_factory() as session:
        for one in (
            event(setup, 1, "armed", entry="1000", stop="900", order_type="stop"),
            event(setup, 1, "triggered", entry="1000", stop="900"),
            event(setup, 1, "closed", exit_price="1500", result_r="5"),
            event(setup, 2, "armed", entry="1100", stop="1000"),
            event(setup, 2, "triggered", entry="1100", stop="1000"),
            event(setup, 2, "closed", exit_price="1000", result_r="-1"),
            event(setup, 3, "armed", entry="1200", stop="1100"),
            event(setup, 3, "cancelled", reason="the setup withdrew it"),
            event(setup, 4, "armed", entry="1300", stop="1200"),
        ):
            record_event(session, one)
        record_event(session, event(setup, 4, "armed", entry="1300", stop="1200"))  # read twice
        session.commit()

    history = client.get(f"/live-setups/{setup['id']}/signals").json()
    metrics = client.get("/live-setups").json()[0]["metrics"]

    assert [(s["number"], s["status"]) for s in history] == [
        (4, "armed"),
        (3, "cancelled"),
        (2, "closed"),
        (1, "closed"),
    ]
    assert history[3]["result_r"] == "5.00000000"
    assert history[1]["reason"] == "the setup withdrew it"
    assert (metrics["signals"], metrics["closed"], metrics["open"], metrics["cancelled"]) == (
        4,
        2,
        1,
        1,
    )
    assert (metrics["wins"], metrics["net_r"], metrics["profit_factor"]) == (1, "4.00000000", "5")
    assert metrics["win_rate"] == "0.5"
    assert metrics["max_drawdown_r"] == "1.00000000"


def test_a_setup_removed_keeps_its_history(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, _ = a_run(session_factory)
    setup = client.post("/live-setups/from-run", json={"backtest_id": str(run)}).json()
    with session_factory() as session:
        record_event(session, event(setup, 7, "armed", entry="1000", stop="900"))
        session.commit()

    removed = client.delete(f"/live-setups/{setup['id']}")
    assert removed.status_code == 204
    with session_factory() as session:
        from tradeforge_db.models import SignalRecord  # noqa: PLC0415

        kept = session.query(SignalRecord).filter_by(number=7).one()
        assert kept.setup_id is None
        assert kept.symbol == "WIN"


CHOCH: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "CHOCH BASE edit test",
    "timeframe": "H1",
    "setup": {
        "type": "structure_choch",
        "params": {
            "htf": None,
            "side": "long",
            "gift_stop": "gift",
            "entry_point": "edge",
            "htf_regions": "any",
            "stop_buffer": 0.15,
            "volume_filter": False,
            "breakeven_at_r": None,
            "allow_secondary": True,
            "min_bars_to_touch": 7,
            "htf_allow_secondary": True,
        },
    },
    "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": 3}}},
    "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1}}},
}


def test_editing_a_setup_runs_a_new_version_and_keeps_the_old_one_in_history(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, first = a_run(session_factory)
    setup = client.post("/live-setups/from-run", json={"backtest_id": str(run)}).json()
    with session_factory() as session:
        record_event(session, event(setup, 9, "armed", entry="1000", stop="900"))
        session.commit()
    edited = {**CHOCH, "setup": {**CHOCH["setup"], "params": {**CHOCH["setup"]["params"]}}}
    edited["setup"]["params"]["side"] = "both"

    response = client.post(f"/live-setups/{setup['id']}/version", json={"definition": edited})
    again = client.post(f"/live-setups/{setup['id']}/version", json={"definition": edited})

    assert response.status_code == 201, response.text
    assert again.status_code == 201, "edited twice from versions of one name: no collision"
    second = response.json()["strategy_id"]
    assert second != str(first)
    with session_factory() as session:
        new = session.get(Strategy, uuid.UUID(second))
        assert new is not None
        assert new.parent_version_id == first
        assert new.definition["setup"]["params"]["side"] == "both"
    [old_signal] = client.get(f"/live-setups/{setup['id']}/signals").json()
    assert old_signal["strategy_id"] == str(first), "a posted signal keeps its version"


def test_an_edit_that_cannot_run_is_refused_and_changes_nothing(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run, first = a_run(session_factory)
    setup = client.post("/live-setups/from-run", json={"backtest_id": str(run)}).json()
    broken = {**CHOCH, "setup": {**CHOCH["setup"], "params": {"side": "sideways"}}}

    response = client.post(f"/live-setups/{setup['id']}/version", json={"definition": broken})

    assert response.status_code == 422
    assert client.get("/live-setups").json()[0]["strategy_id"] == str(first)
