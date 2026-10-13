"""The signals screen by asset, over real Postgres and a Redis double (ADR-0033)."""

import datetime as dt
import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.kill_switch import KillSwitch
from tradeforge_api.live.fleet import RESTARTS_KEY, SUPERVISOR_STATE_KEY, restart_field
from tradeforge_api.main import create_app
from tradeforge_db.live_setups import record_event

from .collector_fakes import running
from .test_live_setups_integration import a_run

pytestmark = pytest.mark.integration


class Store:
    """The two Redis calls the screen makes; `down` makes them raise, as an unreachable Redis."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.down = False

    def get(self, name: str) -> str | None:
        if self.down:
            raise RedisConnectionError("Error 10061 connecting to localhost:6379")
        return self.keys.get(name)

    def hset(self, name: str, key: str, value: str) -> int:
        if self.down:
            raise RedisConnectionError("Error 10061 connecting to localhost:6379")
        self.hashes.setdefault(name, {})[key] = value
        return 1


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def client(session_factory: Callable[[], Session], store: Store) -> Iterator[TestClient]:
    app: Any = create_app(
        settings=Settings(),
        session_factory=session_factory,
        signals_store=store,  # type: ignore[arg-type]
    )
    with TestClient(app) as opened:
        yield opened


def follow(
    client: TestClient, session_factory: Callable[[], Session], *symbols: str
) -> dict[str, Any]:
    """One setup followed on `symbols`, from one run per symbol of the same strategy."""
    run, strategy = a_run(session_factory, symbol=symbols[0])
    setup: dict[str, Any] = client.post(
        "/live-setups/from-run", json={"backtest_id": str(run)}
    ).json()
    for symbol in symbols[1:]:
        more, _ = a_run(session_factory, symbol=symbol, strategy_id=strategy)
        setup = client.post("/live-setups/from-run", json={"backtest_id": str(more)}).json()
    return setup


def market_of(setup: dict[str, Any], symbol: str) -> str:
    return str(next(m["instrument_id"] for m in setup["markets"] if m["symbol"] == symbol))


def said(store: Store, word: dict[str, Any]) -> None:
    store.keys[SUPERVISOR_STATE_KEY] = json.dumps({"at": "2026-10-13T12:00:00+00:00", **word})


def rows(screen: dict[str, Any], symbol: str, timeframe: str = "H1") -> dict[str, dict[str, Any]]:
    asset = next(one for one in screen["assets"] if one["symbol"] == symbol)
    block = next(one for one in asset["timeframes"] if one["timeframe"] == timeframe)
    return {row["name"]: row for row in block["setups"]}


def test_the_screen_reads_asset_then_timeframe_then_setups(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    follow(client, session_factory, "WIN", "WDO")

    screen = client.get("/live-markets").json()

    assert [asset["symbol"] for asset in screen["assets"]] == ["WDO", "WIN"]
    (win_h1,) = screen["assets"][1]["timeframes"]
    assert win_h1["timeframe"] == "H1"
    assert len(win_h1["setups"]) == 1, "one setup, followed on both assets"
    assert screen["supervisor"] == {"alive": False, "at": None, "reachable": True}
    assert win_h1["setups"][0]["state"] == "unknown", "nobody supervises: nothing is known"


def test_each_session_gets_its_state_from_the_supervisor_s_word(
    client: TestClient, session_factory: Callable[[], Session], store: Store
) -> None:
    setup = follow(client, session_factory, "WIN", "WDO")
    win, wdo = market_of(setup, "WIN"), market_of(setup, "WDO")
    said(
        store,
        {
            "processes": [
                {
                    "key": "market:win",
                    "instrument_id": win,
                    "timeframe": "H1",
                    "phase": "live",
                    "heard_at": "2026-10-13T11:59:30+00:00",
                    "sessions": [
                        {"setup_id": setup["id"], "session_id": str(uuid.uuid4()), "asked": False}
                    ],
                }
            ],
            "waiting": [{"setup_id": setup["id"], "instrument_id": wdo, "retry_in": 42}],
            "restarting": [],
        },
    )

    screen = client.get("/live-markets").json()

    assert screen["supervisor"]["alive"] is True
    (on_win,) = rows(screen, "WIN").values()
    (on_wdo,) = rows(screen, "WDO").values()
    assert on_win["state"] == "running"
    assert (on_wdo["state"], on_wdo["retry_in"]) == ("waiting", 42)
    win_block = next(a for a in screen["assets"] if a["symbol"] == "WIN")["timeframes"][0]
    assert [(p["key"], p["phase"], p["sessions"]) for p in win_block["processes"]] == [
        ("market:win", "live", 1)
    ]


def test_signals_under_way_and_the_record_are_each_market_s_own(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    setup = follow(client, session_factory, "WIN", "WDO")

    def event(number: int, kind: str, symbol: str, **fields: str) -> dict[str, str]:
        return {
            "setup_id": setup["id"],
            "strategy_id": setup["strategy_id"],
            "instrument_id": market_of(setup, symbol),
            "number": str(number),
            "kind": kind,
            "symbol": symbol,
            "timeframe": "H1",
            "side": "long",
            "time": "2026-10-13T14:00:00+00:00",
            **fields,
        }

    with session_factory() as session:
        for one in (
            event(1, "armed", "WIN", entry="1000", stop="900", order_type="stop"),
            event(2, "armed", "WIN", entry="1000", stop="900", order_type="stop"),
            event(2, "triggered", "WIN", entry="1000", stop="900"),
            event(2, "closed", "WIN", entry="1000", stop="900", exit_price="1500", result_r="5"),
            event(3, "armed", "WDO", entry="5", stop="4", order_type="stop"),
            event(3, "triggered", "WDO", entry="5", stop="4"),
            event(3, "closed", "WDO", entry="5", stop="4", exit_price="4", result_r="-1"),
        ):
            record_event(session, one)
        session.commit()

    screen = client.get("/live-markets").json()

    (on_win,) = rows(screen, "WIN").values()
    (on_wdo,) = rows(screen, "WDO").values()
    assert [(s["number"], s["status"]) for s in on_win["under_way"]] == [(1, "armed")]
    assert on_wdo["under_way"] == []
    assert on_win["metrics"]["net_r"] == "5.00000000"
    assert on_wdo["metrics"]["net_r"] == "-1.00000000"


def test_a_market_switched_off_is_off_whatever_the_supervisor_says(
    client: TestClient, session_factory: Callable[[], Session], store: Store
) -> None:
    setup = follow(client, session_factory, "WIN")
    win = market_of(setup, "WIN")
    client.patch(f"/live-setups/{setup['id']}/markets/{win}", json={"active": False})
    said(store, {"processes": [], "waiting": [], "restarting": []})

    (row,) = rows(client.get("/live-markets").json(), "WIN").values()

    assert (row["active"], row["state"]) == (False, "off")


def test_restart_asks_the_supervisor_through_one_hash_with_when_it_was_asked(
    client: TestClient, session_factory: Callable[[], Session], store: Store
) -> None:
    setup = follow(client, session_factory, "WIN")
    win = uuid.UUID(market_of(setup, "WIN"))

    response = client.post(
        "/live-markets/restart", json={"instrument_id": str(win), "timeframe": "H1"}
    )

    assert response.status_code == 202, response.text
    asked = store.hashes[RESTARTS_KEY][restart_field(win, "H1")]
    assert dt.datetime.fromisoformat(asked).tzinfo is not None


def test_a_redis_that_cannot_be_read_is_said_and_nothing_is_guessed(
    client: TestClient, session_factory: Callable[[], Session], store: Store
) -> None:
    follow(client, session_factory, "WIN")
    store.down = True

    screen = client.get("/live-markets").json()

    assert screen["supervisor"] == {"alive": False, "at": None, "reachable": False}
    (row,) = rows(screen, "WIN").values()
    assert row["state"] == "unknown"
    restart = client.post(
        "/live-markets/restart", json={"instrument_id": str(uuid.uuid4()), "timeframe": "H1"}
    )
    assert restart.status_code == 503


def test_an_app_with_every_redis_store_injected_starts(
    session_factory: Callable[[], Session], store: Store
) -> None:
    """With all four injected there is no Redis client of the app's own; a default computed from
    one anyway (`getattr(state, name, client)`) failed the start of exactly this app."""
    app: Any = create_app(
        settings=Settings(),
        session_factory=session_factory,
        arq_pool=object(),  # type: ignore[arg-type]
        kill_switch=KillSwitch(store),  # type: ignore[arg-type]
        stop_store=store,  # type: ignore[arg-type]
        collector=running(),
        signals_store=store,  # type: ignore[arg-type]
    )

    with TestClient(app) as opened:
        assert opened.get("/live-markets").status_code == 200
