"""The heavy pages served from snapshots (05/10), over real Postgres.

His report: the PC slowed and the API held 14 GB — an open page of a sweep of 563 thousand runs
asked for a summary that took 75 s, every few seconds, and the best-by-market map took 30 s an
opening. A large sweep is served its last summary, and the map its last ranking; `snapshots`
computes the next ones off the request.
"""

# The sweep suite's `client` and `queue` fixtures are imported by name, which is how pytest finds
# them; each test then takes them as parameters, which ruff reads as redefining the import.
# ruff: noqa: F811

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from tradeforge_api import snapshots
from tradeforge_api.best import BestMetric
from tradeforge_api.routers import sweeps as sweeps_router
from tradeforge_db.models import Sweep

from .test_best_integration import measured
from .test_sweeps_integration import (  # noqa: F401 — fixtures are found by name
    a_sweep_body,
    an_entry,
    client,
    finish,
    queue,
)

pytestmark = pytest.mark.integration


def inline(fn: Callable[..., Any], *args: Any) -> Any:
    """The child process, run here: the same call, without the process."""
    return fn(*args)


def launched(client: Any, *, points: int = 3) -> tuple[str, list[str]]:
    entry = an_entry(
        client,
        name=f"snapshot {uuid.uuid4()}",
        grid={"setup.params.period": [5, 7, 9, 11][:points]},
    )
    sweep_id = client.post("/sweeps", json=a_sweep_body([entry], ["EURUSD"], ["H1"])).json()["id"]
    runs = [row["run"]["id"] for row in client.get(f"/sweeps/{sweep_id}").json()["runs"]]
    return sweep_id, runs


def summary(client: Any, sweep_id: str) -> Any:
    return client.get(f"/sweeps/{sweep_id}", params={"runs": "none"}).json()


class TestALargeSweepIsServedItsLastSummary:
    @pytest.fixture(autouse=True)
    def every_sweep_is_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sweeps_router, "SMALL_SWEEP", 1)

    def test_the_first_read_computes_and_keeps_it_while_runs_are_queued(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, _runs = launched(client)

        body = summary(client, sweep_id)

        assert body["counts"]["queued"] == 3
        assert body["summary_as_of"] is not None
        with session_factory() as session:
            kept = session.get(Sweep, uuid.UUID(sweep_id))
            assert kept is not None
            assert kept.summary is not None
            assert kept.summary["counts"] == body["counts"]

    def test_a_finished_run_moves_the_counts_but_not_the_summary_until_refreshed(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, runs = launched(client)
        first = summary(client, sweep_id)
        finish(session_factory, runs[0], 100)

        stale = summary(client, sweep_id)

        # ⚠️ The counts are live and the summary is the kept one, said with its time — the screen
        # must never present the old numbers as the current ones.
        assert stale["counts"]["done"] == 1
        assert stale["entries"][0]["aggregate"]["points_finished"] == 0
        assert stale["summary_as_of"] == first["summary_as_of"]

        with session_factory() as session:
            assert uuid.UUID(sweep_id) in snapshots.stale_summaries(session)
        snapshots.refresh_summaries(session_factory, inline)

        fresh = summary(client, sweep_id)
        assert fresh["entries"][0]["aggregate"]["points_finished"] == 1
        assert fresh["summary_as_of"] > first["summary_as_of"]
        with session_factory() as session:
            assert uuid.UUID(sweep_id) not in snapshots.stale_summaries(session)

    def test_a_small_sweep_is_never_refreshed_in_the_background(
        self,
        client: Any,
        session_factory: Callable[[], Session],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Below `SMALL_SWEEP` the request computes it, as before: nothing to do here."""
        monkeypatch.setattr(sweeps_router, "SMALL_SWEEP", 100)
        sweep_id, runs = launched(client)
        finish(session_factory, runs[0], 100)

        with session_factory() as session:
            assert uuid.UUID(sweep_id) not in snapshots.stale_summaries(session)
        assert summary(client, sweep_id)["entries"][0]["aggregate"]["points_finished"] == 1


class TestTheBestMapIsServedFromItsSnapshot:
    def test_the_first_opening_computes_it_and_the_next_one_reads_it(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        sweep_id, runs = launched(client, points=1)
        measured(session_factory, runs[0], net_r="1")

        first = client.get("/best/map", params={"metric": "net_r", "every_run": True}).json()
        store = client.app.state.snapshots
        kept = store.read_best(BestMetric.NET_R, every_run=True)

        assert first["as_of"] is not None
        assert kept is not None
        assert [cell["sweep_id"] for cell in first["cells"]] == [sweep_id]

        # ⚠️ Proof the kept one is what is served: a run finishing now does not reach the map.
        _other, more = launched(client, points=1)
        measured(session_factory, more[0], net_r="2")
        again = client.get("/best/map", params={"metric": "net_r", "every_run": True}).json()
        assert again == first

    def test_a_refresh_computes_the_opened_maps_again_only_when_a_run_finished(
        self, client: Any, session_factory: Callable[[], Session]
    ) -> None:
        _sweep_id, runs = launched(client, points=1)
        measured(session_factory, runs[0], net_r="1")
        store = client.app.state.snapshots
        # Opened once: the map it refreshes. Every other one is computed on its first opening.
        client.get("/best/map", params={"metric": "net_r", "every_run": True})

        assert snapshots.refresh_best(session_factory, store, inline) is True
        assert snapshots.refresh_best(session_factory, store, inline) is False
        # ⚠️ Read without `read_best`, which would mark them opened.
        assert store.read_since(BestMetric.SHARPE, every_run=False) is None

        _later, more = launched(client, points=1)
        measured(session_factory, more[0], net_r="2")
        assert snapshots.refresh_best(session_factory, store, inline) is True
        refreshed = store.read_best(BestMetric.NET_R, every_run=True)
        assert refreshed is not None
        assert len(refreshed.cells) == 2


def test_the_child_process_computes_and_keeps_a_summary(
    client: Any, session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real pool: a process of its own, reaching the database from the environment."""
    monkeypatch.setattr(sweeps_router, "SMALL_SWEEP", 1)
    sweep_id, runs = launched(client)
    finish(session_factory, runs[0], 100)

    pool = snapshots.child_pool()
    try:
        pool.submit(snapshots.summary_in_child, sweep_id).result(timeout=120)
    finally:
        pool.shutdown()

    with session_factory() as session:
        kept = session.get(Sweep, uuid.UUID(sweep_id))
        assert kept is not None
        assert kept.summary is not None
        assert kept.summary["counts"]["done"] == 1
