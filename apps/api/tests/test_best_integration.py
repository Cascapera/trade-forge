"""`GET /best/map` and `GET /best/cell` over real Postgres (02/10).

The runs are only queued here and finished by hand, as in the sweep suite: what is under test is
which runs the page ranks and how, not the engine.
"""

# The sweep suite's `client` and `queue` fixtures are imported by name, which is how pytest finds
# them; each test then takes them as parameters, which ruff reads as redefining the import.
# ruff: noqa: F811

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tradeforge_db.models import Backtest, BrokerSymbol

from .test_sweeps_integration import (  # noqa: F401 — fixtures are found by name
    HOUR,
    START,
    a_sweep_body,
    an_entry,
    client,
    finish,
    queue,
    trade_in,
)

pytestmark = pytest.mark.integration


def measured(  # noqa: PLR0913 — keyword-only; each is one measure of the run
    session_factory: Callable[[], Session],
    run_id: str,
    *,
    net_r: str,
    trades: int = 40,
    drawdown: str = "5",
    share: str = "0.5",
) -> None:
    """Finish a run with a record in R — what the page ranks on; again, for one already finished."""
    with session_factory() as session:
        finished = session.get(Backtest, uuid.UUID(run_id))
        assert finished is not None
        if finished.metrics is None:
            finish(session_factory, run_id, int(Decimal(net_r) * 100))
    with session_factory() as session:
        run = session.get(Backtest, uuid.UUID(run_id))
        assert run is not None
        assert run.metrics is not None
        run.metrics.total_trades = trades
        run.metrics.long_trades = trades
        run.metrics.net_r = Decimal(net_r)
        run.metrics.max_drawdown_r = Decimal(drawdown)
        run.metrics.positive_year_share = Decimal(share)
        run.metrics.yearly_r = {"2024": net_r}
        session.commit()


def swept(
    client: Any, session_factory: Callable[[], Session], nets: dict[int, str], **over: Any
) -> tuple[str, str, dict[int, str]]:
    """A finished sweep of EURUSD H1, one point per period, each with the net R given."""
    entry = an_entry(
        client, name=f"best {uuid.uuid4()}", grid={"setup.params.period": sorted(nets)}
    )
    body = a_sweep_body([entry], ["EURUSD"], ["H1"]) | over
    launched = client.post("/sweeps", json=body)
    assert launched.status_code == 202, launched.text
    sweep_id = launched.json()["id"]
    run_of = {
        row["values"]["setup.params.period"]: row["run"]["id"]
        for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]
    }
    for period, net in nets.items():
        measured(session_factory, run_of[period], net_r=net)
    return entry, sweep_id, run_of


class TestTheMap:
    def test_one_cell_per_setup_market_and_chart_holding_its_best_run(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry, sweep_id, run_of = swept(client, session_factory, {5: "10", 7: "30", 9: "20"})

        body = client.get("/best/map", params={"metric": "net_r"}).json()

        (cell,) = [one for one in body["cells"] if one["entry_id"] == entry]
        assert cell["symbol"] == "EURUSD"
        assert cell["timeframe"] == "H1"
        assert cell["run_id"] == run_of[7]
        assert cell["sweep_id"] == sweep_id
        assert Decimal(cell["value"]) == Decimal(30)
        assert cell["ranked"] == 3
        # No broker list in this database: the asset class names the market.
        assert cell["market"] == "Forex"

    def test_the_metric_decides_the_best(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry, _sweep, run_of = swept(client, session_factory, {5: "30", 7: "20"})
        with session_factory() as session:
            for period, drawdown in ((5, "15"), (7, "2")):
                run = session.get(Backtest, uuid.UUID(run_of[period]))
                assert run is not None
                assert run.metrics is not None
                run.metrics.max_drawdown_r = Decimal(drawdown)
            session.commit()

        def best(metric: str) -> str:
            cells = client.get("/best/map", params={"metric": metric}).json()["cells"]
            return str(next(one["run_id"] for one in cells if one["entry_id"] == entry))

        assert best("net_r") == run_of[5]
        assert best("recovery_r") == run_of[7]  # 20 / 2 beats 30 / 15

    def test_below_the_floor_only_when_every_run_is_asked(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry, _sweep, run_of = swept(client, session_factory, {5: "10", 7: "50"})
        measured(session_factory, run_of[7], net_r="50", trades=29)  # H1's floor is 30

        def best(every_run: bool) -> str:
            cells = client.get(
                "/best/map", params={"metric": "net_r", "every_run": every_run}
            ).json()["cells"]
            return str(next(one["run_id"] for one in cells if one["entry_id"] == entry))

        assert best(every_run=False) == run_of[5]
        assert best(every_run=True) == run_of[7]

    def test_tests_training_sweeps_and_older_engines_are_not_ranked(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry, sweep_id, run_of = swept(client, session_factory, {5: "10", 7: "20", 9: "30"})
        with session_factory() as session:
            old = session.get(Backtest, uuid.UUID(run_of[9]))
            assert old is not None
            old.engine_version = "0.0.1"
            session.commit()
        test = client.post(
            f"/sweeps/{sweep_id}/holdout",
            json={
                "date_from": (START + 200 * HOUR).isoformat(),
                "date_to": (START + 300 * HOUR).isoformat(),
                "top_n": 1,
                "metric": "net_r",
            },
        )
        assert test.status_code == 202, test.text
        tested = client.get(f"/sweeps/{test.json()['id']}").json()["runs"]
        for row in tested:
            measured(session_factory, row["run"]["id"], net_r="99")

        cells = client.get("/best/map", params={"metric": "net_r"}).json()["cells"]

        (cell,) = [one for one in cells if one["entry_id"] == entry]
        assert cell["run_id"] == run_of[7]
        assert cell["ranked"] == 2

    def test_the_brokers_tree_names_the_market(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        with session_factory() as session:
            session.add(
                BrokerSymbol(
                    symbol="EURUSD",
                    path="Metals\\EURUSD",
                    visible=True,
                    synced_at=dt.datetime.now(tz=dt.UTC),
                )
            )
            session.commit()
        entry, _sweep, _runs = swept(client, session_factory, {5: "10"})

        cells = client.get("/best/map").json()["cells"]

        assert next(one["market"] for one in cells if one["entry_id"] == entry) == "Metals"


class TestTheCell:
    def test_the_top_runs_skip_near_clones_and_name_their_tests(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        entry, sweep_id, run_of = swept(
            client, session_factory, {5: "10", 7: "40", 9: "30", 11: "20"}
        )
        # 9 opens 7's trades, closed another way: a near-clone (#370/#371). 11 opens its own.
        trade_in(session_factory, run_of[7], ["2", "1"], first=START)
        trade_in(session_factory, run_of[9], ["1", "-1"], first=START)
        trade_in(session_factory, run_of[11], ["1", "1"], first=START + HOUR)
        test = client.post(
            f"/sweeps/{sweep_id}/holdout",
            json={
                "date_from": (START + 200 * HOUR).isoformat(),
                "date_to": (START + 300 * HOUR).isoformat(),
                "top_n": 1,
                "metric": "net_r",
            },
        )
        assert test.status_code == 202, test.text
        (tested,) = client.get(f"/sweeps/{test.json()['id']}").json()["runs"]
        measured(session_factory, tested["run"]["id"], net_r="-3")

        body = client.get(
            "/best/cell",
            params={
                "symbol": "EURUSD",
                "entry_id": entry,
                "timeframe": "H1",
                "metric": "net_r",
                "top_n": 3,
            },
        ).json()

        assert body["ranked"] == 4
        points = body["points"]
        assert [one["run_id"] for one in points] == [run_of[7], run_of[11], run_of[5]]
        assert points[0]["values"]["setup.params.period"] == 7
        assert Decimal(points[0]["net_r"]) == Decimal(40)
        assert points[0]["net_r_per_year"] is not None
        # The best was tested on the reserved window and lost there; the others never were.
        (out,) = points[0]["tests"]
        assert Decimal(out["net_r"]) == Decimal(-3)
        assert out["sweep_id"] == test.json()["id"]
        assert points[1]["tests"] == []

    @pytest.mark.parametrize("field", ["symbol", "entry_id"])
    def test_text_the_database_cannot_store_is_refused_not_a_server_error(
        self, client: Any, field: str
    ) -> None:
        """Schemathesis (02/10): a NUL in a query parameter reached Postgres as a 500."""
        params = {"symbol": "EURUSD", "entry_id": str(uuid.uuid4()), "timeframe": "H1"}
        params[field] = "\0"

        refused = client.get("/best/cell", params=params)

        assert refused.status_code == 422, refused.text

    def test_a_cell_with_nothing_ranked_is_empty_not_an_error(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        body = client.get(
            "/best/cell",
            params={"symbol": "EURUSD", "entry_id": str(uuid.uuid4()), "timeframe": "H1"},
        )

        assert body.status_code == 200
        assert body.json()["points"] == []
        assert body.json()["entry_name"] is None
